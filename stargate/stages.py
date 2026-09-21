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
from .commit import commit_run, commit_summary, terminal_commit_at_head
from .config import (
    SEVERITIES,
    blocking_severities,
    commit_enabled,
    prompt_dirs,
    render_prompt,
    token_cap,
    validate_publication_request,
)
from .core import (
    RunContext,
    StargateError,
    git,
    print_output,
    repo_root,
    run_process,
    split_plan_name,
)
from .detect import detection_mode, selected_test_command
from .publish import (
    PUBLISH_FAILED,
    open_pull_request,
    publication_command,
    push_branch,
    select_remote,
)
from .run import (
    budget_spent,
    complete_stage,
    create_worktree,
    enter_stage,
    inherited_findings,
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


def _task_output_label(ctx: RunContext, phase: str) -> str | None:
    if ctx.mode == "fanout-task":
        return f"task {ctx.slug}/{phase}"
    return None


def _print_test_output(ctx: RunContext, output: str) -> None:
    """Keep a parallel task's complete test transcript atomic and attributable."""
    label = _task_output_label(ctx, "tests")
    if label is None:
        print_output(output, end="" if output.endswith("\n") else "\n")
        return

    prefix = f"[{label}] "
    chunks = output.splitlines(keepends=True)
    labelled = "".join(prefix + chunk for chunk in chunks) if chunks else f"[{label}]"
    print_output(labelled, end="" if labelled.endswith("\n") else "\n")


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
    output_label = _task_output_label(ctx, "tests")
    output_prefix = f"[{output_label}] " if output_label else ""
    if not command:
        print_output(
            "\n"
            f"{output_prefix}No automatic test_command configured; "
            "skipping orchestrator-level tests."
        )
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
    print_output(f"\n{output_prefix}Running {kind} test command: {command}")
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
            output_label=output_label,
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

    _print_test_output(ctx, output)
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
    publish: bool = False,
) -> int:
    if commit:
        needs_terminal_commit = False
        if ctx.mode == "fanout":
            terminal_commit = str(ctx.fanout.get("terminal_commit") or "")
            terminal_verdict = str(ctx.fanout.get("terminal_verdict") or "")
            if terminal_verdict != verdict or not terminal_commit:
                recovered = terminal_commit_at_head(ctx, verdict)
                if recovered:
                    ctx.commit = recovered
                    ctx.fanout["terminal_commit"] = recovered
                    ctx.fanout["terminal_verdict"] = verdict
                else:
                    needs_terminal_commit = True
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
    if commit_outcome != "failed":
        # The run actually finished, so its recorded review/fix cycle stops
        # being a checkpoint. Resuming a finished run is a deliberate request
        # for a fresh verdict -- that is how a corrected reviewer config takes
        # effect. Only a run still blocked on its commit keeps the record.
        ctx.review = {}
    elif ctx.review:
        # Committing stages the tree on its way out, and a staged path leaves
        # the untracked half of the fingerprint. Rebase the record on what is
        # on disk now, so a resume can still tell the reviewed tree from one
        # edited since the verdict.
        ctx.review = {**ctx.review, "fingerprint": worktree_fingerprint(ctx)}
    write_summary(ctx, task, verdict, test_exit, commit)
    save_state(ctx, verdict.lower())

    pushed, published, publish_exit = (
        _publish(ctx, verdict, test_exit, commit_outcome) if publish else ("", "", None)
    )

    print("\n=== RESULT ===")
    print(f"Verdict:   {verdict}")
    print(f"Branch:    {ctx.branch}")
    print(f"Commit:    {_result_commit_summary(ctx, commit)}")
    print(f"Worktree:  {ctx.worktree}")
    if ctx.mode == "fanout":
        print("Review/fix: integration worktree only")
    print(f"Artifacts: {ctx.artifacts}")
    if published.strip():
        print(f"Pull request: {published.strip()}")
    if ctx.tokens_used:
        cap = token_cap(ctx.config)
        print(f"Tokens:    {ctx.tokens_used:,}" + (f" of {cap:,}" if cap else " (no cap)"))
    if ctx.mode == "fanout":
        if ctx.commit:
            print(
                "\nFinal review and fixer passes ran only on the Stargate "
                "integration branch; fixer edits were not copied back to task "
                "branches.\nTask branches were merged only into the Stargate "
                "integration branch.\n",
                end="",
            )
        else:
            task_commits = _fanout_task_commit_count(ctx)
            noun = "commit" if task_commits == 1 else "commits"
            verb = "remains" if task_commits == 1 else "remain"
            print(
                "\nNo integration terminal commit was created; "
                f"{task_commits} completed task {noun} {verb} on the task branches.\n",
                end="",
            )
    elif publish and not ctx.commit:
        print("\nNothing was committed.")
    if pushed:
        print(
            f"\nThe run's branch was pushed to {pushed}; nothing was merged into "
            "your original branch or deleted automatically."
        )
    elif publish:
        # A failed or interrupted push may have reached the remote before its
        # acknowledgement was lost; do not claim that nothing was pushed.
        print("\nNothing was merged into your original branch or deleted automatically.")
    elif ctx.mode == "fanout":
        print(
            "Nothing was merged into your original branch, pushed, or deleted "
            "automatically."
        )
    elif ctx.commit:
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

    if publish_exit is not None:
        return publish_exit
    if commit_outcome == "failed":
        return 5
    if verdict == "BUDGET_EXCEEDED":
        return 4
    if verdict != "APPROVED":
        return 2
    if test_exit not in (None, 0):
        return 3
    return 0


def pull_request_body(ctx: RunContext, verdict: str, test_exit: int | None) -> str:
    if ctx.test_command:
        result = f"exit {test_exit}" if test_exit is not None else "not run"
        tests = f"{ctx.test_command} ({result})"
    else:
        tests = "not run (report-only detection)" if "report-only" in ctx.test_source else (
            "not configured"
        )
    source = f"- **Task source:** {ctx.task_source}\n" if ctx.task_source else ""
    findings = "\n".join(findings_table(ctx.findings)) if ctx.findings else (
        "The reviewer reported no findings."
    )
    # A bullet per field: single newlines would render as one run-on
    # paragraph, and the body is the surface the findings are read on.
    return f"""- **Verdict:** {verdict}
- **Run:** {ctx.run_id}
- **Branch:** {ctx.branch}
- **Base:** {ctx.base_ref} @ {ctx.base_commit[:12]}
- **Commit:** {ctx.commit or 'none'}
{source}- **Tests:** {tests}

## task
{ctx.task}

## findings
{findings}

Produced by stargate agents; publication requested by the operator.
Traces: .stargate/runs/{ctx.run_id}/
"""


def _publish(
    ctx: RunContext, verdict: str, test_exit: int | None, commit_outcome: str,
) -> tuple[str, str, int | None]:
    """Publication failure must leave the terminal verdict and commit intact."""
    print("\n=== PULL REQUEST ===")
    # D20: use task text, never the architect's NAME or parsed source fields.
    first_line = next((line for line in ctx.task.splitlines() if line.strip()), "")
    title = " ".join(first_line.split()) or "stargate run"
    cmd: list[str] = []
    body_path = ctx.artifacts / "pull-request.md"
    remote, pushed = "<remote>", ""
    reason = (
        verdict if verdict != "APPROVED" else
        f"the test command failed (exit {test_exit})" if test_exit not in (None, 0) else
        "the commit failed" if commit_outcome == "failed" else
        "nothing was committed" if not ctx.commit else ""
    )
    publish_exit = None
    try:
        body = pull_request_body(ctx, verdict, test_exit)
        body_path.write_text(body)
        cmd = publication_command(ctx.config, ctx.branch, title)
        if reason:
            print(
                f"Not published: {reason}. "
                "Publishing requires an APPROVED result and passing tests."
            )
            try:
                remote = select_remote(ctx.repo)
            except StargateError:
                pass  # The manual instructions can leave remote selection to the operator.
        else:
            remote = select_remote(ctx.repo)
            push_branch(ctx.repo, remote, ctx.branch, ctx.artifacts)
            pushed = remote
            output = open_pull_request(ctx.config, ctx.repo, cmd, body)
            return pushed, output.strip() or "Opened successfully.", None
    except (StargateError, OSError, ValueError, KeyboardInterrupt) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if pushed:
            print(
                f"The branch is already on {pushed}; it has not been rolled back.", file=sys.stderr,
            )
        publish_exit = (
            128 + int(getattr(exc, "signum", 2)) if isinstance(exc, KeyboardInterrupt)
            else PUBLISH_FAILED if not reason else None
        )
    print(f"The work is on branch {ctx.branch} in {ctx.worktree}.")
    if not cmd:
        print(f"No PR command could be prepared; see the error above. PR body path: {body_path}")
        return pushed, "", publish_exit
    print("To publish it yourself:")
    print(f"  cd {shlex.quote(str(ctx.repo))}")
    if not pushed:
        print(f"  git push {shlex.quote(remote)} {shlex.quote(ctx.branch)}")
    print(f"  {shlex.join(cmd)} < {shlex.quote(str(body_path))}")
    return pushed, "", publish_exit


def write_summary(
    ctx: RunContext,
    task: str,
    verdict: str,
    test_exit: int | None,
    commit: bool,
) -> None:
    if ctx.worktree.exists():
        try:
            status = git(ctx.worktree, "status", "--short").stdout
            diff_stat = git(
                ctx.worktree, "diff", "--stat", ctx.base_commit
            ).stdout
        except StargateError as exc:
            status = f"(Git status unavailable: {exc})"
            diff_stat = "(Git diff unavailable)"
    else:
        status = "(integration worktree not created)"
        diff_stat = "(integration worktree not created)"
    cap = token_cap(ctx.config)
    tokens = f"{ctx.tokens_used:,}" + (f" of {cap:,}" if cap else "")
    fanout_details = ""
    if ctx.mode == "fanout":
        fanout_details = (
            "Mode: fan-out\n"
            "Review/fixer scope: integration worktree only; fixer edits are "
            "not copied back to task branches.\n"
        )
    source_line = f"Task source: {ctx.task_source}\n" if ctx.task_source else ""
    summary = f"""# stargate run

Task: {task}
{source_line}Run: {ctx.run_id}
Base ref: {ctx.base_ref}
Base commit: {ctx.base_commit}
Branch: {ctx.branch}
Commit: {_result_commit_summary(ctx, commit)}
Worktree: {ctx.worktree}
{fanout_details}Verdict: {verdict}
Test command: {ctx.test_command or "(none)"} ({ctx.test_source})
Test exit: {test_exit}
Tokens reported: {tokens}

## git status

{status or "(clean)"}

## diff stat

{diff_stat or "(no tracked diff)"}
"""
    if ctx.findings:
        summary += "\n## findings\n\n" + "\n".join(findings_table(ctx.findings)) + "\n"
    if ctx.mode == "fanout":
        saved_records = ctx.fanout.get("tasks", {})
        records = saved_records if isinstance(saved_records, dict) else {}
        lines = [
            "\n## fan-out tasks\n",
            "| task | status | tests | tokens | commit | error |",
            "|---|---|---:|---:|---|---|",
        ]
        for task_id in ctx.fanout.get("order", []):
            saved_record = records.get(task_id)
            if not isinstance(saved_record, dict):
                lines.append(
                    f"| {_summary_cell(task_id)} | missing | not run | - | - | "
                    "Task record is missing from state.json. |"
                )
                continue
            record = saved_record
            test_exit_value = record.get("test_exit")
            tests = "not run" if test_exit_value is None else str(test_exit_value)
            reported = _summary_tokens(record.get("tokens_used"))
            task_commit = str(record.get("commit") or "")
            error = str(record.get("error") or "-")
            lines.append(
                f"| {_summary_cell(task_id)} | "
                f"{_summary_cell(record.get('status', 'unknown'))} | "
                f"{_summary_cell(tests)} | {_summary_cell(reported)} | "
                f"{_summary_cell(task_commit[:12] or '-')} | "
                f"{_summary_cell(error)} |"
            )
        summary += "\n".join(lines) + "\n"
    (ctx.artifacts / "summary.md").write_text(summary)


def findings_table(findings: list[Any]) -> list[str]:
    lines = ["| severity | where | finding |", "|---|---|---|"]
    for entry in _by_severity(findings):
        if not isinstance(entry, dict):
            # Only reachable from hand-edited or externally written state.
            # Showing it beats raising while writing the terminal report.
            lines.append(f"| ? | - | {_summary_cell(entry)} |")
            continue
        where = str(entry.get("file") or "-")
        if entry.get("line") is not None:
            where += f":{entry['line']}"
        detail = str(entry.get("finding", ""))
        if entry.get("why"):
            detail += f" (why: {entry['why']})"
        lines.append(
            f"| {_summary_cell(entry.get('severity', '?'))} | "
            f"{_summary_cell(where)} | {_summary_cell(detail)} |"
        )
    return lines


def _fanout_task_commit_count(ctx: RunContext) -> int:
    saved_records = ctx.fanout.get("tasks", {})
    if not isinstance(saved_records, dict):
        return 0
    return sum(
        isinstance(record, dict)
        and isinstance(record.get("commit"), str)
        and bool(record["commit"])
        for record in saved_records.values()
    )


def _result_commit_summary(ctx: RunContext, enabled: bool) -> str:
    if ctx.mode != "fanout" or enabled:
        return commit_summary(ctx, enabled)
    task_commits = _fanout_task_commit_count(ctx)
    noun = "commit" if task_commits == 1 else "commits"
    return (
        "none (no integration terminal commit; "
        f"{task_commits} task {noun} preserved)"
    )


def _by_severity(findings: list[Any]) -> list[Any]:
    """Highest severity first, reviewer order kept inside each severity.

    Total by construction. Parsing guarantees the shape for anything this run
    produced, but `findings` is restored straight from state.json, and a
    corrupted or externally written entry must not raise here -- this runs
    while writing the terminal report, after the commit has been attempted.
    """
    def rank(entry: Any) -> int:
        severity = entry.get("severity") if isinstance(entry, dict) else None
        return (
            SEVERITIES.index(severity)
            if severity in SEVERITIES
            else len(SEVERITIES)
        )

    return sorted(findings, key=rank)


def _known_findings_section(ctx: RunContext) -> str:
    """Include the previous review's context, or leave the prompt unchanged."""
    run_id, findings = inherited_findings(ctx.repo, ctx.base_ref)
    if not findings:
        return ""
    lines = [
        "",
        "## Known unresolved findings",
        "",
        f"These are the findings of the last completed review of run {run_id}, "
        f"whose branch {ctx.base_ref} is this run's base ref.",
        "",
        "They describe the tree that review saw, not necessarily the tree you "
        "are reading: an edit made after it -- a fixer pass that ran before the "
        "run stopped -- may already have resolved one. Nothing here was "
        "re-checked. Verify each against the current code, then plan the fix or "
        "say why it stays.",
        "",
    ]
    for entry in _by_severity(findings):
        if not isinstance(entry, dict):
            # Hand-edited state may contain junk; report it without failing.
            lines.append("- [?] " + str(entry).replace("\n", " "))
            continue
        # `file` and `line` are independently optional, so a line without a
        # file is valid data. Dropping it here would hide it, and the summary
        # table already renders that pair as `-:42`.
        where = str(entry.get("file") or "")
        if entry.get("line") is not None:
            where = f"{where or '-'}:{entry['line']}"
        detail = str(entry.get("finding", ""))
        if entry.get("why"):
            detail += f" (why: {entry['why']})"
        severity = str(entry.get("severity", "?"))
        lines.append(f"- [{severity}] " + (f"{where} -- " if where else "") + detail)
    return "\n".join(lines) + "\n"


def _summary_cell(value: object) -> str:
    """Keep persisted diagnostic text inside one Markdown table cell."""
    return str(value).replace("|", "\\|").replace("\r\n", "\n").replace(
        "\n", "<br>"
    )


def _summary_tokens(value: object) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value:,}"
    return "-" if value is None else str(value)


def _resume_context(
    repo: Path,
    args: argparse.Namespace,
    config: dict[str, Any],
) -> RunContext:
    """Load resume state once mode has been validated for safe dispatch."""
    state_path = repo / ".stargate" / "runs" / args.run_id / "state.json"
    if not state_path.exists():
        raise StargateError(f"No run state at {state_path}")
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError) as exc:
        raise StargateError(
            f"Cannot resume {args.run_id}: unreadable state.json ({exc})"
        ) from exc
    if not isinstance(state, dict):
        raise StargateError(
            f"Cannot resume {args.run_id}: state.json is not a JSON object."
        )
    # Runs written before mode was persisted were necessarily linear. Keep
    # those histories resumable while still rejecting an explicit bad mode.
    mode = state["mode"] if "mode" in state else "linear"
    if mode not in ("linear", "fanout"):
        raise StargateError(
            f"Cannot resume {args.run_id}: state.json has no valid mode "
            "(expected 'linear' or 'fanout')."
        )
    try:
        return load_run(
            repo, args.run_id, config, use_frozen=args.config is None
        )
    except (OSError, TypeError, ValueError, KeyError) as exc:
        raise StargateError(
            f"Cannot resume {args.run_id}: invalid state.json ({exc})"
        ) from exc


def _write_failed_fanout_summary(ctx: RunContext | None) -> None:
    """Leave a failed fan-out run the summary artifact every other run gets."""
    if ctx is None:
        # The failure preceded orchestration, so no run is ours to describe.
        return
    try:
        write_summary(ctx, ctx.task, "FAILED", None, True)
    except (StargateError, OSError, TypeError, ValueError, KeyError):
        # The original orchestration failure remains the actionable error.
        return


def orchestrate(args: argparse.Namespace, script_dir: Path, config: dict[str, Any]) -> int:
    repo = repo_root(Path.cwd())
    resuming = args.command == "resume"

    fanout = bool(getattr(args, "fan_out", False))
    ctx = None
    if resuming:
        ctx = _resume_context(repo, args, config)
        fanout = ctx.mode == "fanout"
    effective_config = ctx.config if ctx is not None else config
    validate_publication_request(effective_config, requested=getattr(args, "pr", False))
    if fanout:
        from .fanout import orchestrate_fanout

        orchestrated: list[RunContext] = []
        try:
            return orchestrate_fanout(
                args, script_dir, config, repo, on_context=orchestrated.append
            )
        except StargateError:
            _write_failed_fanout_summary(
                orchestrated[-1] if orchestrated else None
            )
            raise

    if resuming:
        assert ctx is not None
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
        blocking_severities(config)
        warn_if_dirty(repo)
        ctx = make_context(
            repo, config, args.task, args.base_ref, args.name,
            task_source=getattr(args, "from_url", None) or "",
        )
        prompts = snapshot(ctx, prompt_dirs(config, script_dir))

    commit = commit_enabled(ctx.config) and not args.no_commit
    blocking = blocking_severities(ctx.config)

    print(f"\nRun ID:   {ctx.run_id}")
    print(f"Base:     {ctx.base_ref} @ {ctx.base_commit[:12]}")
    print(f"Branch:   {ctx.branch}")
    print(f"Worktree: {ctx.worktree}")
    print(f"Artifacts:{ctx.artifacts}")
    if blocking:
        print(f"Review:   blocking severities: {', '.join(blocking)}")

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
    publish = getattr(args, "pr", False)
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
            prompts,
            "architect",
            task=ctx.task,
            base_ref=ctx.base_ref,
            known_findings=_known_findings_section(ctx),
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
        return finish(ctx, ctx.task, "BUDGET_EXCEEDED", None, commit=commit, publish=publish)

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


VERDICTS = ("APPROVED", "CHANGES_REQUESTED")


def _policy_verdict(
    verdict: str, findings: list[dict[str, Any]], contract: str,
    blocking: tuple[str, ...],
) -> str:
    # Prose has no findings contract; empty JSON has no evidence for derivation.
    if not blocking or contract != "json" or not findings:
        return verdict
    return "CHANGES_REQUESTED" if any(
        entry["severity"] in blocking for entry in findings
    ) else "APPROVED"


def parse_review(raw: str) -> tuple[str, list[dict[str, Any]], str]:
    """The reviewer's verdict, findings, and the contract that spoke.

    A JSON object is the current contract; a response whose last line is
    `VERDICT: ...` is the original one, still spoken by a frozen prompt from a
    run created before this change and by any custom reviewer.md. The verdict
    stands exactly as the reviewer gave it either way -- nothing here derives
    it from the findings.
    """
    text = raw.strip()
    data: Any = None
    # A ```json fence is the most common shape a model emits when asked for an
    # object, and throwing away a finished, paid review over three backticks
    # costs a rerun -- the same reasoning as the trailing-note tolerance below.
    # Only the opening fence is removed: raw_decode already stops at the end of
    # the object and ignores the closing fence, exactly as it ignores trailing
    # prose. The prose contract further down still sees the untouched response.
    candidate = text.partition("\n")[2].strip() if text.startswith("```") else text
    if candidate.startswith("{"):
        try:
            # raw_decode, not loads: a real reviewer completed the object and
            # then appended a note about what it could not verify, and throwing
            # away a finished, paid review over trailing prose costs a rerun.
            # Still not "find JSON in prose" -- the object must start the
            # response, and anything after it is discarded.
            data, _ = json.JSONDecoder().raw_decode(candidate)
        except json.JSONDecodeError:
            data = None
    if isinstance(data, dict):
        return (*_parse_json_review(data), "json")

    # The verdict is the last line, not a substring anywhere in the prose:
    # "I cannot give VERDICT: APPROVED because..." must not read as approval.
    last_line = text.splitlines()[-1].strip() if text else ""
    for verdict in VERDICTS:
        if last_line == f"VERDICT: {verdict}":
            return verdict, [], "prose"
    raise StargateError(
        "Reviewer output is neither a JSON review object nor a response "
        f"ending in exactly 'VERDICT: {VERDICTS[0]}' or "
        f"'VERDICT: {VERDICTS[1]}'."
    )


def _parse_json_review(data: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    verdict = data.get("verdict")
    if verdict not in VERDICTS:
        raise StargateError(
            "Reviewer JSON needs a 'verdict' of exactly "
            f"{' or '.join(VERDICTS)}; got {verdict!r}."
        )
    raw_findings = data.get("findings", [])
    if not isinstance(raw_findings, list):
        raise StargateError("Reviewer JSON 'findings' must be a list.")

    findings: list[dict[str, Any]] = []
    for index, item in enumerate(raw_findings):
        where = f"findings[{index}]"
        if not isinstance(item, dict):
            raise StargateError(f"Reviewer JSON {where} must be an object.")
        severity = item.get("severity")
        if not isinstance(severity, str) or severity.lower() not in SEVERITIES:
            raise StargateError(
                f"Reviewer JSON {where}.severity must be one of "
                f"{', '.join(SEVERITIES)}; got {severity!r}."
            )
        finding = item.get("finding")
        if not isinstance(finding, str) or not finding.strip():
            raise StargateError(
                f"Reviewer JSON {where}.finding must be a non-empty string."
            )
        line = item.get("line")
        # bool is a subclass of int: `"line": true` must not pass as a number.
        if line is not None and (
            isinstance(line, bool) or not isinstance(line, int)
        ):
            raise StargateError(
                f"Reviewer JSON {where}.line must be an integer when present."
            )
        entry: dict[str, Any] = {
            "severity": severity.lower(),
            "finding": finding.strip(),
        }
        for key in ("file", "why"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                entry[key] = value.strip()
        if line is not None:
            entry["line"] = line
        findings.append(entry)
    # Parsing preserves the explicit verdict. Opt-in policy is applied separately.
    return verdict, findings


def _record_review(
    ctx: RunContext, attempt: int, verdict: str, *, fixed: bool = False
) -> None:
    """Persist where the review/fix loop got to, so a resume re-enters it.

    The fingerprint is the tree the reviewer actually judged. A resume trusts
    the recorded verdict only while the worktree still hashes to it.
    """
    ctx.review = {
        "attempt": attempt,
        "verdict": verdict,
        "fingerprint": worktree_fingerprint(ctx),
        "fixed": fixed,
    }
    if blocking := blocking_severities(ctx.config):
        ctx.review["blocking_severities"] = list(blocking)
    save_state(ctx, "running")


def _recorded_review(
    ctx: RunContext, attempt: int, blocking: tuple[str, ...]
) -> tuple[str, str] | None:
    """Re-evaluate a saved pass under the effective (possibly replaced) policy."""
    try:
        review = (ctx.artifacts / f"review-{attempt}.md").read_text().strip()
    except OSError:
        return None
    try:
        verdict, findings, contract = parse_review(review)
    except StargateError:
        # A truncated or hand-edited artifact cannot stand in for a reviewer.
        return None
    return _policy_verdict(verdict, findings, contract, blocking), review


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

    publish = getattr(args, "pr", False)
    # 4. Review/fix loop.
    configured_loops = int(
        ctx.config.get("settings", {}).get("max_review_loops", 2)
    )
    max_loops = args.max_review_loops if args.max_review_loops is not None else configured_loops
    blocking = blocking_severities(ctx.config)
    verdict = "CHANGES_REQUESTED"

    # A resume must not buy a second opinion on a tree a reviewer already
    # judged: that costs a full review and can return a different finding set
    # than the one whose fixer was interrupted. The recorded cycle stands only
    # while the worktree still hashes to what that reviewer saw.
    recorded = ctx.review if isinstance(ctx.review, dict) else {}
    recorded_attempt = int(recorded.get("attempt") or 0)
    recorded_verdict = str(recorded.get("verdict") or "")
    unchanged = bool(recorded_attempt) and str(
        recorded.get("fingerprint") or ""
    ) == worktree_fingerprint(ctx)

    start = 0
    pending: str | None = None
    if recorded_attempt and bool(recorded.get("fixed")):
        # That pass's fixer finished, so the tree moved on and the next review
        # is owed. Continuing the count keeps max_review_loops a budget for the
        # run rather than a fresh allowance for every resume of it.
        start = recorded_attempt
    elif unchanged and recorded_verdict in VERDICTS:
        # A disabled policy can change a verdict only if this checkpoint used
        # one. Without either policy, retain V1's artifact replay rules.
        policy_applies = bool(blocking or recorded.get("blocking_severities"))
        replayed = (
            _recorded_review(ctx, recorded_attempt, blocking)
            if policy_applies or recorded_verdict == "CHANGES_REQUESTED" else None
        )
        if recorded_verdict == "APPROVED" and (
            not policy_applies or (replayed is not None and replayed[0] == "APPROVED")
        ):
            # V1 retries the commit without reopening the approved artifact.
            # If a policy was involved, remember the effective policy for a
            # later resume, including its removal.
            if policy_applies:
                _record_review(ctx, recorded_attempt, "APPROVED")
            print(f"\n=== REVIEW {recorded_attempt} (skipped, already APPROVED) ===")
            complete_stage(ctx, "review")
            return finish(ctx, task, "APPROVED", test_exit, commit=commit, publish=publish)
        if replayed is not None and (policy_applies or replayed[0] == "CHANGES_REQUESTED"):
            pending = replayed[1]
            start = recorded_attempt - 1

    enter_stage(ctx, "review")
    for attempt in range(start, max_loops + 1):
        if pending is not None:
            review, pending = pending, None
            print(
                f"\n=== REVIEW {attempt + 1} (skipped, reusing "
                f"{ctx.artifacts / f'review-{attempt + 1}.md'}) ==="
            )
        else:
            if budget_spent(ctx, f"review {attempt + 1}"):
                return finish(
                    ctx, task, "BUDGET_EXCEEDED", test_exit, commit=commit, publish=publish
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

        artifact = ctx.artifacts / f"review-{attempt + 1}.md"
        try:
            reviewed, ctx.findings, contract = parse_review(review)
        except StargateError as exc:
            raise StargateError(f"{exc} See {artifact}") from exc
        verdict = _policy_verdict(reviewed, ctx.findings, contract, blocking)
        if verdict != reviewed:
            print(
                f"[review {attempt + 1}] settings.blocking_severities "
                f"({', '.join(blocking)}) derived {verdict}; the reviewer said {reviewed}."
            )
        if verdict == "APPROVED":
            _record_review(ctx, attempt + 1, "APPROVED")
            break

        _record_review(ctx, attempt + 1, "CHANGES_REQUESTED")
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
        if blocking:
            fixer_prompt += (
                "\n\nORCHESTRATOR NOTE (separate from the review above):\n"
                f"settings.blocking_severities is active: {', '.join(blocking)}. "
                "JSON reviews with findings block only on these severities; other "
                "findings are recorded but do not block. Prose and reviews without "
                "findings keep the reviewer's explicit verdict.\n"
            )
        if budget_spent(ctx, f"fixer {attempt + 1}"):
            return finish(
                ctx, task, "BUDGET_EXCEEDED", test_exit, commit=commit, publish=publish
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
        _record_review(ctx, attempt + 1, "CHANGES_REQUESTED", fixed=True)
        test_exit, test_report = run_tests(ctx, f"fix-{attempt + 1}")

    complete_stage(ctx, "review")
    return finish(ctx, task, verdict, test_exit, commit=commit, publish=publish)
