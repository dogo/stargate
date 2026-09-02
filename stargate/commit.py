"""Committing a finished run on its own branch, and saying why when that fails."""
from __future__ import annotations

import shlex
import subprocess
import sys

from .core import RunContext, StargateError, git, git_quiet, run_process


COMMIT_SUBJECT_CHARS = 72


COMMIT_TIMEOUT_SECONDS = 600


COMMIT_OUTPUT_LINES = 20


def commit_message(ctx: RunContext, verdict: str, test_exit: int | None) -> str:
    task = " ".join(ctx.task.split()) or "run"
    suffix = f" ({verdict})"
    available = max(0, COMMIT_SUBJECT_CHARS - len("stargate: ") - len(suffix))
    subject_task = task[:available].rstrip()
    subject = f"stargate: {subject_task}{suffix}"

    if ctx.test_command:
        tests = (
            f"{ctx.test_command} (exit {test_exit})"
            if test_exit is not None else f"{ctx.test_command} (not run)"
        )
    elif "report-only" in ctx.test_source:
        tests = "not run (report-only detection)"
    else:
        tests = "not configured"

    trailers = [
        f"Stargate-Run-Id: {ctx.run_id}",
        f"Stargate-Verdict: {verdict}",
        f"Stargate-Base-Ref: {ctx.base_ref}",
        f"Stargate-Base-Commit: {ctx.base_commit}",
    ]
    if test_exit is not None:
        trailers.append(f"Stargate-Tests-Exit: {test_exit}")
    return f"""\
{subject}

Task: {task}
Verdict: {verdict}
Tests: {tests}
Base: {ctx.base_ref}
Base commit: {ctx.base_commit}

Produced by stargate agents, committed by the orchestrator; the agents
themselves never run git. Plan, review and traces:
.stargate/runs/{ctx.run_id}/

{chr(10).join(trailers)}
"""


def stage_run_changes(ctx: RunContext) -> bool:
    """Stage agent work while excluding untracked test-created entries."""
    excludes = [f":(exclude,literal){name}" for name in sorted(ctx.test_artifacts)]
    git(ctx.worktree, "add", "-A", "--", ".", *excludes, capture=True)
    proc = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=str(ctx.worktree),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode not in (0, 1):
        raise StargateError(
            f"git diff --cached --quiet failed in {ctx.worktree}: "
            f"{proc.stderr.strip()}"
        )
    return proc.returncode == 1


def commit_failure(ctx: RunContext, reason: str, output: str = "") -> None:
    tail = "\n".join(output.strip().splitlines()[-COMMIT_OUTPUT_LINES:])
    message = (
        f"Could not commit the run's work ({reason}). The changes are intact "
        f"in {ctx.worktree}; any successfully staged changes remain staged, "
        "and nothing was lost. A pre-commit hook or a commit signing "
        "configuration in this repository is the usual cause -- a hook that "
        "rewrote files may leave those rewrites unstaged. Finish by hand with:\n"
        f"  cd {shlex.quote(str(ctx.worktree))} && git commit"
    )
    if tail:
        message += "\nLast output:\n" + "\n".join(
            f"  {line}" for line in tail.splitlines()
        )
    ctx.commit_error = message
    print(f"\n{message}", file=sys.stderr)


def commit_run(ctx: RunContext, verdict: str, test_exit: int | None) -> str:
    """Make at most one commit after the run reaches a terminal verdict.

    Red results are committed too: they are the results most in need of a
    durable recovery point, and the private, unpushed branch plus verdict in
    the message keeps the history honest. Fixer passes are one editing session,
    so committing before the final review would create misleading checkpoints.
    """
    ctx.commit_error = ""
    if not ctx.worktree.exists():
        return "empty"

    branch = git_quiet(ctx.worktree, "rev-parse", "--abbrev-ref", "HEAD").strip()
    if branch != ctx.branch:
        commit_failure(
            ctx,
            f"worktree is on {branch!r}, not the run branch {ctx.branch!r}",
        )
        return "failed"

    try:
        changed = stage_run_changes(ctx)
    except StargateError as exc:
        commit_failure(ctx, f"staging failed: {exc}")
        return "failed"
    if not changed:
        return "empty"

    message_path = ctx.artifacts / "commit-message.txt"
    message_path.write_text(commit_message(ctx, verdict, test_exit))
    try:
        proc = run_process(
            ["git", "commit", "-F", str(message_path)],
            ctx.worktree,
            check=False,
            timeout=COMMIT_TIMEOUT_SECONDS,
        )
    except StargateError as exc:
        commit_failure(ctx, str(exc))
        return "failed"
    if proc.returncode:
        commit_failure(ctx, f"git exit {proc.returncode}", proc.stdout or "")
        return "failed"

    ctx.commit = git_quiet(ctx.worktree, "rev-parse", "HEAD").strip()
    # Known test output is deliberately left untracked, so it must not be
    # blamed on a hook. The same exclusions reveal only unexpected rewrites.
    excludes = [f":(exclude,literal){name}" for name in sorted(ctx.test_artifacts)]
    status = git_quiet(
        ctx.worktree, "status", "--porcelain", "--", ".", *excludes
    )
    if status:
        print(
            "\nWarning: the commit succeeded, but a repository hook modified "
            f"files afterward. Those changes remain uncommitted in {ctx.worktree}; "
            "stargate will not create a second commit.",
            file=sys.stderr,
        )
    return "committed"


def commit_summary(ctx: RunContext, enabled: bool) -> str:
    if not enabled:
        return "disabled"
    if ctx.commit_error:
        return "FAILED"
    if ctx.commit:
        return ctx.commit
    return "none (nothing to commit)"
