"""Committing a finished run on its own branch, and saying why when that fails."""
from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

from .core import RunContext, StargateError, git_quiet, print_output, run_process

COMMIT_SUBJECT_CHARS = 72


COMMIT_TIMEOUT_SECONDS = 600


COMMIT_OUTPUT_LINES = 20


def _commit_output_label(ctx: RunContext) -> str | None:
    if ctx.mode == "fanout-task":
        return f"task {ctx.slug}/commit"
    return None


def _labelled_output(label: str, output: str) -> str:
    prefix = f"[{label}] "
    chunks = output.splitlines(keepends=True)
    return "".join(prefix + chunk for chunk in chunks) if chunks else f"[{label}]"


def _print_commit_output(ctx: RunContext, output: str) -> None:
    if not output:
        return
    label = _commit_output_label(ctx)
    if label:
        output = _labelled_output(label, output)
    print_output(output, end="" if output.endswith("\n") else "\n")


def _print_commit_diagnostic(ctx: RunContext, message: str) -> None:
    label = _commit_output_label(ctx)
    displayed = _labelled_output(label, message) if label else message
    print_output(f"\n{displayed}", file=sys.stderr)


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
    run_process(
        ["git", "add", "-A", "--", ".", *excludes],
        ctx.worktree,
        output_label=_commit_output_label(ctx),
    )
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
    _print_commit_diagnostic(ctx, message)


def commit_run(
    ctx: RunContext,
    verdict: str,
    test_exit: int | None,
    *,
    allow_empty: bool = False,
) -> str:
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
    if not changed and not allow_empty:
        return "empty"

    message_path = ctx.artifacts / "commit-message.txt"
    message_path.write_text(commit_message(ctx, verdict, test_exit))
    output_label = _commit_output_label(ctx)
    output_log: Path | None = None
    if output_label:
        # A log-backed process is tracked by the signal-safe runner. Its
        # transcript is then printed as one labelled block and removed; the
        # durable commit message remains the only new task artifact.
        output_log = ctx.artifacts / ".commit-output.log"
        output_log.unlink(missing_ok=True)
    try:
        proc = run_process(
            [
                "git",
                "commit",
                *(("--allow-empty",) if allow_empty else ()),
                "-F",
                str(message_path),
            ],
            ctx.worktree,
            check=False,
            timeout=COMMIT_TIMEOUT_SECONDS,
            log_path=output_log,
            output_label=output_label,
        )
    except StargateError as exc:
        commit_failure(ctx, str(exc))
        return "failed"
    finally:
        if output_log is not None:
            output_log.unlink(missing_ok=True)
    if output_label:
        _print_commit_output(ctx, proc.stdout or "")
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
        warning = (
            "Warning: the commit succeeded, but a repository hook modified "
            f"files afterward. Those changes remain uncommitted in {ctx.worktree}; "
            "stargate will not create a second commit."
        )
        _print_commit_diagnostic(ctx, warning)
    return "committed"


def terminal_commit_at_head(ctx: RunContext, verdict: str) -> str:
    """Recover a terminal commit made just before state persistence stopped.

    A signal can arrive after ``git commit`` succeeds but before ``finish``
    records the terminal fields in state.json.  The commit trailers make that
    narrow window distinguishable from both task commits and user commits, so
    a resume can adopt the commit instead of adding another empty one.
    """
    if not ctx.worktree.exists():
        return ""
    try:
        head = git_quiet(ctx.worktree, "rev-parse", "HEAD").strip()
        message = git_quiet(ctx.worktree, "log", "-1", "--format=%B", "HEAD")
    except StargateError:
        return ""
    trailers = set(message.splitlines())
    if (
        f"Stargate-Run-Id: {ctx.run_id}" in trailers
        and f"Stargate-Verdict: {verdict}" in trailers
    ):
        return head
    return ""


def commit_summary(ctx: RunContext, enabled: bool) -> str:
    if not enabled:
        return "disabled"
    if ctx.commit_error:
        return "FAILED"
    if ctx.commit:
        return ctx.commit
    return "none (nothing to commit)"
