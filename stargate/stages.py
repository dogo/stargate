"""The workflow itself: architect, developer, the review loop, and the tests
and summary that bracket them."""
from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any

from .agent import invoke_agent
from .commit import commit_run, commit_summary
from .config import (
    commit_enabled,
    prompt_dirs,
    render_prompt,
    token_cap,
)
from .core import (
    RunContext,
    StargateError,
    git,
    repo_root,
    run_process,
    split_plan_name,
)
from .detect import detection_mode, selected_test_command
from .run import (
    budget_spent,
    complete_stage,
    create_worktree,
    enter_stage,
    load_run,
    make_context,
    save_state,
    snapshot,
    unique_branch,
    untracked_entries,
    warn_if_dirty,
    worktree_fingerprint,
)

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
        artifact = ctx.artifacts / f"tests-{label}.txt"
        proc = run_process(
            ["/bin/sh", "-lc", command],
            ctx.worktree,
            check=False,
            timeout=timeout,
            log_path=artifact,
            timeout_is_error=False,
        )
        output, code = proc.stdout or "", proc.returncode
        if code == 124:
            output += f"\n\n[timed out after {timeout}s]"
    finally:
        # Test suites commonly leave .venv/, *.egg-info/, target/ or
        # node_modules/. A tidy project ignores them, but an incomplete
        # .gitignore must not turn build output into history. Tracked rewrites
        # remain eligible because they are part of the diff the reviewer saw.
        # Persisting these names matters when a crash and resume span processes.
        ctx.test_artifacts |= untracked_entries(ctx.worktree) - before
        save_state(ctx, "running")

    print(output, end="" if output.endswith("\n") else "\n")
    artifact.write_text(
        f"$ {command}\n\n{output}\n\nexit_code={code}\n"
    )

    # Agents only need the tail; a full suite log blows the prompt budget.
    tail = "\n".join(output.splitlines()[-TEST_TAIL_LINES:])
    verdict = "PASSED" if code == 0 else f"FAILED (exit {code})"
    return code, f"$ {command}\n{verdict}\n\n{tail}"


def finish(
    ctx: RunContext,
    task: str,
    verdict: str,
    test_exit: int | None,
    *,
    commit: bool,
) -> int:
    if commit:
        needs_terminal_commit = (
            ctx.mode == "fanout"
            and ctx.fanout.get("terminal_verdict") != verdict
        )
        commit_outcome = commit_run(
            ctx, verdict, test_exit, allow_empty=needs_terminal_commit
        )
        if ctx.mode == "fanout" and commit_outcome == "committed":
            ctx.fanout["terminal_commit"] = ctx.commit
            ctx.fanout["terminal_verdict"] = verdict
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
        if ctx.mode == "fanout":
            print(
                "\nTask branches were merged only into the Stargate integration "
                "branch.\nNothing was merged into your original branch, pushed, "
                "or deleted automatically."
            )
        else:
            print("\nNothing was merged, pushed, or deleted automatically.")
    else:
        print("\nNothing was committed, merged, pushed, or deleted automatically.")
    print(
        f"Inspect with: cd {shlex.quote(str(ctx.worktree))} "
        f"&& git status && git diff {shlex.quote(ctx.base_commit)}"
    )
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
    cap = token_cap(ctx.config)
    tokens = f"{ctx.tokens_used:,}" + (f" of {cap:,}" if cap else "")
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
Tokens reported: {tokens}

## git status

{status or "(clean)"}

## diff stat

{diff_stat or "(no tracked diff)"}
"""
    if ctx.mode == "fanout":
        records = ctx.fanout.get("tasks", {})
        lines = ["\n## fan-out tasks\n", "| task | status | tests | commit |", "|---|---|---:|---|"]
        for task_id in ctx.fanout.get("order", []):
            record = records.get(task_id, {})
            test_exit_value = record.get("test_exit")
            tests = "not run" if test_exit_value is None else str(test_exit_value)
            task_commit = str(record.get("commit") or "")
            lines.append(
                f"| {task_id} | {record.get('status', 'unknown')} | {tests} | "
                f"{task_commit[:12] or '-'} |"
            )
        summary += "\n".join(lines) + "\n"
    (ctx.artifacts / "summary.md").write_text(summary)


def orchestrate(args: argparse.Namespace, script_dir: Path, config: dict[str, Any]) -> int:
    repo = repo_root(Path.cwd())
    resuming = args.command == "resume"

    fanout = bool(getattr(args, "fan_out", False))
    if resuming:
        state_path = repo / ".stargate" / "runs" / args.run_id / "state.json"
        if state_path.exists():
            try:
                fanout = json.loads(state_path.read_text()).get("mode") == "fanout"
            except (OSError, ValueError):
                # load_run below owns the detailed error path for malformed state.
                pass
    if fanout:
        from .fanout import orchestrate_fanout

        return orchestrate_fanout(args, script_dir, config, repo)

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

    return review_and_finish(
        ctx,
        args,
        prompts,
        task=ctx.task,
        plan=plan,
        test_exit=test_exit,
        test_report=test_report,
        commit=commit,
    )


def review_and_finish(
    ctx: RunContext,
    args: argparse.Namespace,
    prompts: list[Path],
    *,
    task: str,
    plan: str,
    test_exit: int | None,
    test_report: str,
    commit: bool,
) -> int:
    """Run the shared review/fix tail for a linear or integrated worktree."""

    # 4. Review/fix loop.
    configured_loops = int(
        ctx.config.get("settings", {}).get("max_review_loops", 2)
    )
    max_loops = args.max_review_loops if args.max_review_loops is not None else configured_loops
    verdict = "CHANGES_REQUESTED"

    # ponytail: resume always re-runs the review loop from the first attempt.
    # Re-reviewing is idempotent and cheap next to re-implementing; per-attempt
    # resume would need every fixer pass recorded separately.
    enter_stage(ctx, "review")
    for attempt in range(max_loops + 1):
        if budget_spent(ctx, f"review {attempt + 1}"):
            return finish(
                ctx, task, "BUDGET_EXCEEDED", test_exit, commit=commit
            )
        review_prompt = render_prompt(
            prompts,
            "reviewer",
            task=task,
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
            task=task,
            base_ref=ctx.base_ref,
            plan=plan,
            review=review,
            tests=test_report,
        )
        if budget_spent(ctx, f"fixer {attempt + 1}"):
            return finish(
                ctx, task, "BUDGET_EXCEEDED", test_exit, commit=commit
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
    return finish(ctx, task, verdict, test_exit, commit=commit)
