#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


from .config import (
    commit_enabled,
    PROJECT_CONFIG,
    agent_command,
    agent_entry,
    agent_env,
    expand_test_command,
    init_config,
    init_prompts,
    load_config,
    parse_usage,
    prompt_dirs,
    render_prompt,
    resolve_config,
    retry_settings,
    token_cap,
)
from .run import (
    unique_branch,
    REDOABLE_STAGES,
    budget_spent,
    clean_runs,
    complete_stage,
    create_worktree,
    enter_stage,
    list_runs,
    load_run,
    make_context,
    save_state,
    snapshot,
    untracked_entries,
    warn_if_dirty,
    worktree_fingerprint,
)
from .detect import detection_mode, selected_test_command
from .doctor import doctor
from .core import (
    git_quiet,
    RunContext,
    StargateError,
    Terminated,
    git,
    repo_root,
    run_process,
    split_plan_name,
)


FINGERPRINT_LINES = 20


def record_usage(ctx: RunContext, role: str, transcript: str) -> None:
    """Charge one completed attempt, including one the vendor rejected late."""
    used = parse_usage(
        transcript, agent_entry(ctx.config, role).get("usage_pattern")
    )
    ctx.tokens_used += used
    if used:
        cap = token_cap(ctx.config)
        budget = f" of {cap:,}" if cap else ""
        print(
            f"\n[{role}] reported {used:,} tokens; "
            f"{ctx.tokens_used:,}{budget} used so far."
        )


def attempt_log_path(output_path: Path, attempt: int) -> Path:
    # Attempt one keeps the historical path, so retries-off runs retain the
    # same artifacts while later failures cannot overwrite its evidence.
    suffix = "" if attempt == 1 else f".attempt-{attempt}"
    return output_path.with_name(output_path.name + suffix + ".log")


def failure_fingerprint(error: StargateError, trace: str) -> tuple[str, str]:
    """Normalize per-attempt noise without interpreting vendor error text."""
    # The prefix is ours and distinguishes timeout from a non-zero exit. The
    # rest includes attempt-specific trace paths and the full command, neither
    # of which says whether another attempt has a chance of succeeding.
    reason = str(error).split(" (", 1)[0]
    tail = "\n".join(trace.strip().splitlines()[-FINGERPRINT_LINES:])
    return re.sub(r"\d+", "#", reason), re.sub(r"\d+", "#", tail)


def invoke_agent(
    ctx: RunContext,
    role: str,
    prompt: str,
    cwd: Path,
    output_path: Path,
) -> str:
    """Run one agent and return its FINAL MESSAGE, not its stdout.

    The distinction matters: `codex exec` streams the whole session — reasoning,
    every command it ran, a token footer — to stdout. Forwarding that as {plan}
    or {review} makes each hop pay for the previous hop's trace. An agent whose
    command contains "{output}" is handed a file path to write its last message
    to, and that file is what gets forwarded; its stdout is kept as a .log.

    Retries stay inside this single invocation so no completed role is replayed.
    Each attempt has its own trace, both for debugging and to count any usage
    the failed process reported exactly once.
    """
    cmd = agent_command(ctx.config, role)
    writes_final = any("{output}" in part for part in cmd)
    # Expand {output} first so those literal characters inside a configured
    # test command cannot unexpectedly become a path.
    cmd = [part.replace("{output}", str(output_path)) for part in cmd]
    cmd = expand_test_command(cmd, ctx.test_command)
    timeout = float(ctx.config.get("settings", {}).get("agent_timeout_seconds", 1800))
    env = agent_env(agent_entry(ctx.config, role))
    retries, backoff = retry_settings(ctx.config)
    attempts = retries + 1
    previous_failure: tuple[str, str] | None = None

    for attempt in range(1, attempts + 1):
        log_path = attempt_log_path(output_path, attempt)
        print(f"trace: tail -f {shlex.quote(str(log_path))}", flush=True)

        # A retry or explicitly redone stage must not inherit an earlier
        # answer and pass the output contract after writing nothing.
        if writes_final and output_path.exists():
            output_path.unlink()

        started = time.monotonic()
        try:
            proc = run_process(
                [*cmd, prompt], cwd, timeout=timeout or None, log_path=log_path,
                env=env,
            )
        except OSError as exc:
            # A process that cannot be started will fail the same way after a
            # backoff; unlike an agent exit, it never made a remote request.
            raise StargateError(
                f"Could not start the agent for role '{role}': {exc}"
            ) from exc
        except StargateError as exc:
            if retries:
                trace = log_path.read_text() if log_path.exists() else ""
                record_usage(ctx, role, trace)
                print(
                    f"\n[{role}] attempt {attempt} of {attempts} failed: {exc}",
                    flush=True,
                )
            else:
                # Keeping retries disabled must preserve the original failure
                # path, including terminal output and token accounting.
                raise

            if attempt == attempts:
                raise

            fingerprint = failure_fingerprint(exc, trace)
            if fingerprint == previous_failure:
                remaining = attempts - attempt
                print(
                    f"[{role}] failed identically twice; not retrying "
                    f"{remaining} more time(s).",
                    flush=True,
                )
                raise
            previous_failure = fingerprint

            wait = backoff * 2 ** (attempt - 1)
            print(
                f"[{role}] retrying in {wait:g}s "
                f"(attempt {attempt + 1} of {attempts}).",
                flush=True,
            )
            time.sleep(wait)
            continue

        transcript = proc.stdout or ""
        print(
            f"\n[{role}] exit {proc.returncode} in "
            f"{time.monotonic() - started:.0f}s",
            flush=True,
        )
        record_usage(ctx, role, transcript)
        break

    if not writes_final:
        output_path.write_text(transcript)
        return transcript

    final = output_path.read_text() if output_path.exists() else ""
    if not final.strip():
        raise StargateError(
            f"Agent for role '{role}' declares {{output}} but wrote nothing to "
            f"{output_path}. Check that its CLI supports the flag you passed."
        )
    return final


TEST_TAIL_LINES = 200


def plan_tests(ctx: RunContext) -> None:
    """Choose once from the original repository and explain the choice."""
    settings = ctx.config.get("settings", {})
    mode = detection_mode(ctx.config)
    configured = str(settings.get("test_command", "") or "").strip()
    ctx.test_command, ctx.detected = selected_test_command(ctx.config, ctx.repo)
    ctx.test_source = ""
    if configured:
        ctx.test_source = "settings.test_command"
        print(f"Tests:    {configured}   (settings.test_command)")
        return
    if mode == "off":
        ctx.test_source = "not configured; detection off"
        print("Tests:    none configured (test command detection is off)")
        return

    if not ctx.detected:
        ctx.test_source = "not configured; none detected"
        print("Tests:    none configured, none detected")
        return

    selected = ctx.detected[0]
    if mode == "auto":
        ctx.test_source = f"detected: {selected.source}"
        print(
            f"Tests:    {selected.command}   (detected: {selected.source}; "
            "automatic)"
        )
        for candidate in ctx.detected[1:]:
            print(
                f"          {candidate.command}   (detected: {candidate.source}; "
                "lower priority, not selected)"
            )
        return

    ctx.test_source = (
        f"detected {selected.command} from {selected.source}; report-only"
    )
    print(
        f"Tests:    {selected.command}   (detected: {selected.source}; not run -- "
        "settings.test_command_detection: report)"
    )
    for candidate in ctx.detected[1:]:
        print(
            f"          {candidate.command}   (detected: {candidate.source}; not run)"
        )
    print("          To confirm the first candidate, add to .stargate.yaml:")
    print("            settings:")
    print(f"              test_command: {selected.command!r}")


def run_tests(ctx: RunContext, label: str) -> tuple[int | None, str]:
    """Run the configured suite. Returns (exit code, report shown to agents)."""
    settings = ctx.config.get("settings", {})
    command = ctx.test_command
    if not command:
        print("\nNo automatic test_command configured; skipping orchestrator-level tests.")
        if ctx.detected:
            candidate = ctx.detected[0]
            return None, (
                "No test command is configured in the orchestrator. It detected "
                f"`{candidate.command}` ({candidate.source}) but did not run it. "
                "Run the project's own tests yourself if you can."
            )
        return None, (
            "No test command is configured in the orchestrator. "
            "Run the project's own tests yourself if you can."
        )

    kind = "detected" if ctx.test_source.startswith("detected:") else "configured"
    print(f"\nRunning {kind} test command: {command}")
    timeout = float(settings.get("test_timeout_seconds", 900)) or None
    before = untracked_entries(ctx.worktree)
    try:
        try:
            proc = subprocess.run(
                ["/bin/sh", "-lc", command],
                cwd=str(ctx.worktree),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
            )
            output, code = proc.stdout or "", proc.returncode
        except subprocess.TimeoutExpired as exc:
            output = (exc.output or "") + f"\n\n[timed out after {timeout}s]"
            code = 124
    finally:
        # Test suites commonly leave .venv/, *.egg-info/, target/ or
        # node_modules/. A tidy project ignores them, but an incomplete
        # .gitignore must not turn build output into history. Tracked rewrites
        # remain eligible because they are part of the diff the reviewer saw.
        # Persisting these names matters when a crash and resume span processes.
        ctx.test_artifacts |= untracked_entries(ctx.worktree) - before
        save_state(ctx, "running")

    print(output, end="" if output.endswith("\n") else "\n")
    (ctx.artifacts / f"tests-{label}.txt").write_text(
        f"$ {command}\n\n{output}\n\nexit_code={code}\n"
    )

    # Agents only need the tail; a full suite log blows the prompt budget.
    tail = "\n".join(output.splitlines()[-TEST_TAIL_LINES:])
    verdict = "PASSED" if code == 0 else f"FAILED (exit {code})"
    return code, f"$ {command}\n{verdict}\n\n{tail}"


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


def finish(
    ctx: RunContext,
    task: str,
    verdict: str,
    test_exit: int | None,
    *,
    commit: bool,
) -> int:
    if commit:
        commit_outcome = commit_run(ctx, verdict, test_exit)
    else:
        # A failed commit belongs to the invocation that attempted it. If the
        # user deliberately resumes without committing, preserving that error
        # would make a successful terminal result keep returning exit 5.
        ctx.commit_error = ""
        commit_outcome = "disabled"
    write_summary(ctx, task, verdict, test_exit, commit)
    save_state(ctx, verdict.lower())

    print("\n=== RESULT ===")
    print(f"Verdict:   {verdict}")
    print(f"Branch:    {ctx.branch}")
    print(f"Commit:    {commit_summary(ctx, commit)}")
    print(f"Worktree:  {ctx.worktree}")
    print(f"Artifacts: {ctx.artifacts}")
    if ctx.tokens_used:
        cap = token_cap(ctx.config)
        print(f"Tokens:    {ctx.tokens_used:,}" + (f" of {cap:,}" if cap else " (no cap)"))
    if ctx.commit:
        print("\nNothing was merged, pushed, or deleted automatically.")
    else:
        print("\nNothing was committed, merged, pushed, or deleted automatically.")
    print(f"Inspect with: cd {shlex.quote(str(ctx.worktree))} && git status && git diff {shlex.quote(ctx.base_commit)}")
    if ctx.commit:
        print(
            "History: git log --oneline "
            f"{shlex.quote(ctx.base_commit + '..' + ctx.branch)}"
        )
        print(
            "Build on it with: stargate run --base-ref "
            f"{shlex.quote(ctx.branch)} \"<next task>\""
        )

    if commit_outcome == "failed":
        return 5
    if verdict == "BUDGET_EXCEEDED":
        return 4
    if verdict != "APPROVED":
        return 2
    if test_exit not in (None, 0):
        return 3
    return 0


def write_summary(
    ctx: RunContext,
    task: str,
    verdict: str,
    test_exit: int | None,
    commit: bool,
) -> None:
    status = git(ctx.worktree, "status", "--short").stdout
    diff_stat = git(ctx.worktree, "diff", "--stat", ctx.base_commit).stdout
    summary = f"""# stargate run

Task: {task}
Run: {ctx.run_id}
Base ref: {ctx.base_ref}
Base commit: {ctx.base_commit}
Branch: {ctx.branch}
Commit: {commit_summary(ctx, commit)}
Worktree: {ctx.worktree}
Verdict: {verdict}
Test command: {ctx.test_command or "(none)"} ({ctx.test_source})
Test exit: {test_exit}
Tokens reported: {ctx.tokens_used:,}{" of " + format(token_cap(ctx.config), ",") if token_cap(ctx.config) else ""}

## git status

{status or "(clean)"}

## diff stat

{diff_stat or "(no tracked diff)"}
"""
    (ctx.artifacts / "summary.md").write_text(summary)


def orchestrate(args: argparse.Namespace, script_dir: Path, config: dict[str, Any]) -> int:
    repo = repo_root(Path.cwd())
    resuming = args.command == "resume"

    if resuming:
        ctx = load_run(repo, args.run_id, config, use_frozen=args.config is None)
        prompts = [ctx.artifacts / "prompts"]
        print(f"\nResuming {ctx.run_id}: {ctx.task}")
        if redo := set(args.redo):
            # Completion is the only reason a stage is skipped, so forgetting
            # that record replaces hand-editing state.json.
            ctx.done -= redo
            print(f"Redoing: {', '.join(sorted(redo))}")
            if "architect" in redo and "developer" in ctx.done:
                print(
                    "Warning: the developer is still marked complete, so the "
                    "new plan will not be implemented. Add --redo developer.",
                    file=sys.stderr,
                )
        print(f"Completed: {', '.join(sorted(ctx.done)) or '(nothing)'}")
    else:
        warn_if_dirty(repo)
        ctx = make_context(repo, config, args.task, args.base_ref, args.name)
        prompts = snapshot(ctx, prompt_dirs(config, script_dir))

    commit = commit_enabled(ctx.config) and not args.no_commit

    print(f"\nRun ID:   {ctx.run_id}")
    print(f"Base:     {ctx.base_ref} @ {ctx.base_commit[:12]}")
    print(f"Branch:   {ctx.branch}")
    print(f"Worktree: {ctx.worktree}")
    print(f"Artifacts:{ctx.artifacts}")

    try:
        plan_tests(ctx)
        return run_stages(ctx, args, prompts, commit=commit)
    except (StargateError, KeyboardInterrupt) as exc:
        save_state(ctx, "failed", f"{type(exc).__name__}: {exc}")
        print(f"\nResume with: stargate resume {ctx.run_id}", file=sys.stderr)
        raise


def run_stages(
    ctx: RunContext,
    args: argparse.Namespace,
    prompts: list[Path],
    *,
    commit: bool,
) -> int:
    config = ctx.config
    plan_path = ctx.artifacts / "plan.md"
    architect_ran = False

    # 1. Architect reads the original repository and emits a plan.
    if "architect" in ctx.done:
        raw_plan = plan_path.read_text().strip()
        print(f"\n=== ARCHITECT (skipped, reusing {plan_path}) ===")
    else:
        architect_ran = True
        enter_stage(ctx, "architect")
        architect_prompt = render_prompt(
            prompts, "architect", task=ctx.task, base_ref=ctx.base_ref
        )
        print("\n=== ARCHITECT ===")
        raw_plan = invoke_agent(
            ctx, "architect", architect_prompt, ctx.repo, plan_path
        ).strip()

    architect_name, plan = split_plan_name(raw_plan)
    if not plan:
        raise StargateError("Architect returned an empty plan.")
    # The worktree is deliberately created after planning, so the branch can
    # still adopt the architect's vocabulary. The run id was already printed
    # and names artifacts/worktree paths; changing it here would break resume.
    # An existing worktree is stronger evidence than a missing completion bit
    # after a crash, and must never be orphaned by a rename.
    if (
        architect_name and not ctx.named_by_user and ctx.tag
        and "worktree" not in ctx.done and not ctx.worktree.exists()
    ):
        proposed = f"stargate/{architect_name}-{ctx.tag}"
        ctx.branch = unique_branch(ctx.repo, proposed)
        save_state(ctx, "running")
        print(f"Branch:   {ctx.branch}   (named by the architect)")
    if architect_ran:
        complete_stage(ctx, "architect")

    # 2. Create isolated implementation branch/worktree.
    enter_stage(ctx, "worktree")
    print("\n=== WORKTREE ===")
    create_worktree(ctx)
    complete_stage(ctx, "worktree")

    if budget_spent(ctx, "the developer"):
        return finish(ctx, ctx.task, "BUDGET_EXCEEDED", None, commit=commit)

    # 3. Developer implements.
    if "developer" in ctx.done:
        print("\n=== DEVELOPER (skipped, already ran in this run) ===")
    else:
        enter_stage(ctx, "developer")
        before = worktree_fingerprint(ctx)
        developer_prompt = render_prompt(
            prompts,
            "developer",
            task=ctx.task,
            base_ref=ctx.base_ref,
            plan=plan,
        )
        print("\n=== DEVELOPER ===")
        invoke_agent(
            ctx,
            "developer",
            developer_prompt,
            ctx.worktree,
            ctx.artifacts / "developer.txt",
        )
        if worktree_fingerprint(ctx) == before:
            # Leaving this incomplete is intentional: a reviewer can only
            # rediscover the empty diff, while resume must rerun this stage.
            raise StargateError(
                f"The developer changed nothing in {ctx.worktree}; there is "
                f"nothing to review. Check the trace in "
                f"{ctx.artifacts / 'developer.txt.log'}, verify the agent's "
                "tools with 'stargate doctor --probe', then resume: this stage "
                "was not recorded as complete."
            )
        complete_stage(ctx, "developer")

    test_exit, test_report = run_tests(ctx, "developer")

    # 4. Review/fix loop.
    configured_loops = int(config.get("settings", {}).get("max_review_loops", 2))
    max_loops = args.max_review_loops if args.max_review_loops is not None else configured_loops
    verdict = "CHANGES_REQUESTED"

    # ponytail: resume always re-runs the review loop from the first attempt.
    # Re-reviewing is idempotent and cheap next to re-implementing; per-attempt
    # resume would need every fixer pass recorded separately.
    enter_stage(ctx, "review")
    for attempt in range(max_loops + 1):
        if budget_spent(ctx, f"review {attempt + 1}"):
            return finish(
                ctx, ctx.task, "BUDGET_EXCEEDED", test_exit, commit=commit
            )
        review_prompt = render_prompt(
            prompts,
            "reviewer",
            task=ctx.task,
            base_ref=ctx.base_ref,
            plan=plan,
            tests=test_report,
        )
        print(f"\n=== REVIEW {attempt + 1} ===")
        review = invoke_agent(
            ctx,
            "reviewer",
            review_prompt,
            ctx.worktree,
            ctx.artifacts / f"review-{attempt + 1}.md",
        ).strip()

        # The verdict is the last line, not a substring anywhere in the prose:
        # "I cannot give VERDICT: APPROVED because..." must not read as approval.
        last_line = review.splitlines()[-1].strip() if review else ""
        if last_line == "VERDICT: APPROVED":
            verdict = "APPROVED"
            break

        if last_line != "VERDICT: CHANGES_REQUESTED":
            raise StargateError(
                "Reviewer did not end its response with a recognized verdict. "
                f"See {ctx.artifacts / f'review-{attempt + 1}.md'}"
            )

        if attempt >= max_loops:
            verdict = "CHANGES_REQUESTED"
            break

        fixer_prompt = render_prompt(
            prompts,
            "fixer",
            task=ctx.task,
            base_ref=ctx.base_ref,
            plan=plan,
            review=review,
            tests=test_report,
        )
        if budget_spent(ctx, f"fixer {attempt + 1}"):
            return finish(
                ctx, ctx.task, "BUDGET_EXCEEDED", test_exit, commit=commit
            )

        before = worktree_fingerprint(ctx)
        print(f"\n=== FIXER {attempt + 1} ===")
        invoke_agent(
            ctx,
            "fixer",
            fixer_prompt,
            ctx.worktree,
            ctx.artifacts / f"fix-{attempt + 1}.txt",
        )
        if worktree_fingerprint(ctx) == before:
            # A fixer may reasonably reject a review finding, but another
            # review of the identical tree would only repeat the same verdict.
            print(
                f"\n[fixer {attempt + 1}] changed nothing; a further review "
                "would reach the same verdict. Stopping the review loop.",
                file=sys.stderr,
            )
            break
        test_exit, test_report = run_tests(ctx, f"fix-{attempt + 1}")

    complete_stage(ctx, "review")
    return finish(ctx, ctx.task, verdict, test_exit, commit=commit)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stargate",
        description="Tiny Claude Code + Codex CLI multi-agent orchestrator.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help=f"Complete standalone config. Without it, ./{PROJECT_CONFIG}, "
        "the user config and packaged defaults are layered.",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    doctor_parser = sub.add_parser(
        "doctor", help="Check local CLI dependencies and configuration."
    )
    doctor_parser.add_argument(
        "--probe", action="store_true",
        help="Make one real, potentially billable call to each unique agent.",
    )
    sub.add_parser(
        "init-config",
        help="Copy the packaged agents.yaml to ~/.config/stargate/agents.yaml.",
    )
    sub.add_parser(
        "init-prompts",
        help="Copy the packaged prompts to ~/.config/stargate/prompts/ so they "
        "can be edited without touching the install.",
    )
    sub.add_parser(
        "list", aliases=["runs"],
        help="List the runs recorded in this repository, newest first."
    )
    clean = sub.add_parser(
        "clean", help="Remove a run's merged branch, clean worktree and artifacts."
    )
    clean.add_argument("run_id", nargs="?", help="Run ID shown by 'stargate list'.")
    clean.add_argument(
        "--all", action="store_true", dest="all_runs",
        help="Clean every recorded run that passes the safety checks.",
    )

    run = sub.add_parser("run", help="Plan, implement, review and fix a task.")
    run.add_argument("task", help="Feature/bug/task description.")
    run.add_argument(
        "--base-ref",
        default=None,
        help="Git ref to branch from. Defaults to the current branch/ref.",
    )
    run.add_argument(
        "--name",
        default=None,
        help="Short name for the branch and run id, e.g. --name 'passkey auth'. "
        "Overrides the name the architect suggests.",
    )

    resume = sub.add_parser(
        "resume",
        help="Continue a run that failed partway, reusing its plan, worktree, "
        "config and prompts.",
    )
    resume.add_argument("run_id", help="Run ID, as printed by the original run.")
    resume.add_argument(
        "--redo", action="append", default=[], choices=REDOABLE_STAGES,
        metavar="STAGE",
        help="Run this completed stage again instead of skipping it "
        f"({', '.join(REDOABLE_STAGES)}). Repeatable.",
    )

    for parser_ in (run, resume):
        parser_.add_argument(
            "--no-commit",
            action="store_true",
            help="Leave the run's work uncommitted in the worktree "
            "(overrides settings.commit).",
        )
        parser_.add_argument(
            "--max-review-loops",
            type=int,
            default=None,
            help="Override settings.max_review_loops.",
        )
    return parser


def install_signal_handlers() -> None:
    """Turn catchable termination into the existing resumable failure path."""
    handled = [
        signum for name in ("SIGTERM", "SIGHUP")
        if (signum := getattr(signal, name, None)) is not None
    ]

    def terminate(signum: int, _frame: Any) -> None:
        # One shot lets a second signal terminate even if cleanup gets stuck.
        for handled_signum in handled:
            signal.signal(handled_signum, signal.SIG_DFL)
        raise Terminated(signum)

    for signum in handled:
        signal.signal(signum, terminate)


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "init-config":
        return init_config(script_dir)
    if args.command == "init-prompts":
        return init_prompts(script_dir)

    try:
        if args.command in ("run", "resume"):
            # SIGINT already becomes KeyboardInterrupt; replacing it would add
            # a second path for an interrupt that is already recorded safely.
            install_signal_handlers()

        if args.command in ("list", "runs"):
            return list_runs(repo_root(Path.cwd()))
        if args.command == "clean":
            return clean_runs(repo_root(Path.cwd()), args.run_id, args.all_runs)

        config_paths = resolve_config(args.config, script_dir)
        config, layers = load_config(config_paths)
        if args.command == "doctor":
            return doctor(
                config,
                layers,
                script_dir,
                probe=args.probe,
                explicit_config=args.config is not None,
            )
        if args.command in ("run", "resume"):
            return orchestrate(args, script_dir, config)
        parser.error("Unknown command")
        return 2
    except StargateError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt as exc:
        signum = getattr(exc, "signum", signal.SIGINT)
        message = "Interrupted" if signum == signal.SIGINT else "Terminated"
        print(f"\n{message}.", file=sys.stderr)
        return 128 + int(signum)
