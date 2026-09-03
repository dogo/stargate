"""Fan-out runs: architect a DAG, execute ready nodes, then review integration."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .agent import invoke_agent
from .commit import commit_run
from .config import commit_enabled, prompt_dirs, render_prompt
from .core import RunContext, StargateError, git_quiet, short_name
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
    warn_if_dirty,
    worktree_fingerprint,
)
from .stages import plan_tests, review_and_finish, run_tests

TASK_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,47}\Z")
MAX_FANOUT_TASKS_DEFAULT = 8
MAX_PARALLEL_TASKS_DEFAULT = 2


@dataclass(frozen=True)
class FanoutTask:
    id: str
    task: str
    depends_on: tuple[str, ...]
    acceptance: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task": self.task,
            "depends_on": list(self.depends_on),
            "acceptance": list(self.acceptance),
        }


def _positive_setting(config: dict[str, Any], key: str, default: int) -> int:
    value = config.get("settings", {}).get(key, default)
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise StargateError(f"settings.{key} must be a positive integer.") from exc
    if parsed < 1:
        raise StargateError(f"settings.{key} must be a positive integer.")
    return parsed


def fanout_limits(
    config: dict[str, Any], parallel_override: int | None = None
) -> tuple[int, int]:
    max_tasks = _positive_setting(
        config, "max_fanout_tasks", MAX_FANOUT_TASKS_DEFAULT
    )
    max_parallel = (
        parallel_override
        if parallel_override is not None
        else _positive_setting(
            config, "max_parallel_tasks", MAX_PARALLEL_TASKS_DEFAULT
        )
    )
    if max_parallel < 1:
        raise StargateError("--max-parallel-tasks must be a positive integer.")
    return max_tasks, min(max_parallel, max_tasks)


def parse_task_graph(
    raw: str, max_tasks: int | None
) -> tuple[str, list[FanoutTask]]:
    """Validate the architect's JSON and return tasks in topological order."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StargateError(
            "Fan-out architect did not return valid JSON "
            f"(line {exc.lineno}, column {exc.colno}): {exc.msg}"
        ) from exc
    if not isinstance(data, dict):
        raise StargateError("Fan-out architect output must be one JSON object.")

    name_value = data.get("name")
    name = name_value.strip() if isinstance(name_value, str) else ""
    if not short_name(name):
        raise StargateError("Fan-out architect output needs a usable string 'name'.")

    raw_tasks = data.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise StargateError("Fan-out architect output needs a non-empty 'tasks' list.")
    if max_tasks is not None and len(raw_tasks) > max_tasks:
        raise StargateError(
            f"Fan-out architect returned {len(raw_tasks)} tasks; "
            f"settings.max_fanout_tasks is {max_tasks}."
        )

    tasks: list[FanoutTask] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_tasks, 1):
        where = f"tasks[{index - 1}]"
        if not isinstance(item, dict):
            raise StargateError(f"{where} must be an object.")
        task_id = item.get("id")
        if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
            raise StargateError(
                f"{where}.id must match [a-z0-9][a-z0-9-]{{0,47}}."
            )
        if task_id in seen:
            raise StargateError(f"Duplicate fan-out task id: {task_id!r}.")
        seen.add(task_id)

        description = item.get("task")
        if not isinstance(description, str) or not description.strip():
            raise StargateError(f"{where}.task must be a non-empty string.")
        dependencies = item.get("depends_on", [])
        if not isinstance(dependencies, list) or not all(
            isinstance(value, str) for value in dependencies
        ):
            raise StargateError(f"{where}.depends_on must be a list of task IDs.")
        if len(set(dependencies)) != len(dependencies):
            raise StargateError(f"{where}.depends_on contains a duplicate task ID.")
        acceptance = item.get("acceptance", [])
        if not isinstance(acceptance, list) or not all(
            isinstance(value, str) and value.strip() for value in acceptance
        ):
            raise StargateError(f"{where}.acceptance must be a list of strings.")
        tasks.append(
            FanoutTask(
                id=task_id,
                task=description.strip(),
                depends_on=tuple(dependencies),
                acceptance=tuple(value.strip() for value in acceptance),
            )
        )

    by_id = {task.id: task for task in tasks}
    for task in tasks:
        missing = [dep for dep in task.depends_on if dep not in by_id]
        if missing:
            raise StargateError(
                f"Fan-out task {task.id!r} depends on unknown task(s): "
                f"{', '.join(missing)}."
            )
        if task.id in task.depends_on:
            raise StargateError(f"Fan-out task {task.id!r} depends on itself.")

    position = {task.id: index for index, task in enumerate(tasks)}
    indegree = {task.id: len(task.depends_on) for task in tasks}
    children: dict[str, list[str]] = {task.id: [] for task in tasks}
    for task in tasks:
        for dependency in task.depends_on:
            children[dependency].append(task.id)
    ready = [task.id for task in tasks if indegree[task.id] == 0]
    order: list[str] = []
    while ready:
        ready.sort(key=position.__getitem__)
        task_id = ready.pop(0)
        order.append(task_id)
        for child in children[task_id]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
    if len(order) != len(tasks):
        cyclic = [task_id for task_id, degree in indegree.items() if degree]
        raise StargateError(
            "Fan-out task dependencies contain a cycle involving: "
            f"{', '.join(cyclic)}."
        )
    return name, [by_id[task_id] for task_id in order]


def normalized_graph(name: str, tasks: list[FanoutTask]) -> str:
    return json.dumps(
        {"name": name, "tasks": [task.as_dict() for task in tasks]},
        indent=2,
    ) + "\n"


def _validate_frozen_graph(
    ctx: RunContext, name: str, tasks: list[FanoutTask]
) -> None:
    if not ctx.fanout:
        return
    expected_order = ctx.fanout.get("order")
    records = ctx.fanout.get("tasks")
    if (
        ctx.fanout.get("name") != name
        or
        expected_order != [task.id for task in tasks]
        or not isinstance(records, dict)
    ):
        raise StargateError(
            "tasks.json no longer matches the fan-out graph frozen in state.json."
        )
    for task in tasks:
        record = records.get(task.id)
        if not isinstance(record, dict) or any(
            record.get(key) != value
            for key, value in task.as_dict().items()
        ):
            raise StargateError(
                "tasks.json no longer matches the fan-out graph frozen in state.json."
            )


def _new_task_records(
    ctx: RunContext, name: str, tasks: list[FanoutTask]
) -> dict[str, Any]:
    records: dict[str, dict[str, Any]] = {}
    for task in tasks:
        branch = unique_branch(ctx.repo, f"{ctx.branch}-{task.id}")
        records[task.id] = {
            **task.as_dict(),
            "status": "pending",
            "stage": "pending",
            "branch": branch,
            "worktree": str(ctx.worktree.parent / f"{ctx.run_id}-{task.id}"),
            "base_commit": "",
            "commit": "",
            "test_exit": None,
            "tokens_used": 0,
            "test_artifacts": [],
            "error": None,
        }
    return {
        "name": name,
        "order": [task.id for task in tasks],
        "tasks": records,
        "integrated": [],
    }


def _merge(worktree: Path, branch: str, purpose: str) -> None:
    proc = subprocess.run(
        ["git", "merge", "--no-edit", branch],
        cwd=str(worktree),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if proc.stdout:
        print(proc.stdout, end="" if proc.stdout.endswith("\n") else "\n")
    if proc.returncode == 0:
        return
    subprocess.run(
        ["git", "merge", "--abort"],
        cwd=str(worktree),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    tail = "\n".join((proc.stdout or "").strip().splitlines()[-20:])
    raise StargateError(
        f"Could not merge {branch!r} while {purpose}."
        + (f"\nLast Git output:\n{tail}" if tail else "")
    )


def _task_context(ctx: RunContext, record: dict[str, Any]) -> RunContext:
    task_artifacts = ctx.artifacts / "tasks" / record["id"]
    task_artifacts.mkdir(parents=True, exist_ok=True)
    task_ctx = RunContext(
        repo=ctx.repo,
        config=ctx.config,
        run_id=ctx.run_id,
        slug=record["id"],
        branch=record["branch"],
        base_ref=ctx.base_ref,
        base_commit=record["base_commit"] or ctx.base_commit,
        worktree=Path(record["worktree"]),
        artifacts=task_artifacts,
        task=record["task"],
        tokens_used=int(record.get("tokens_used", 0)),
        test_artifacts=set(record.get("test_artifacts", [])),
        commit=str(record.get("commit") or ""),
        mode="fanout-task",
    )
    task_ctx.test_command = ctx.test_command
    task_ctx.test_source = ctx.test_source
    task_ctx.detected = ctx.detected
    return task_ctx


def _prepare_task(ctx: RunContext, record: dict[str, Any]) -> RunContext:
    dependencies = [ctx.fanout["tasks"][item] for item in record["depends_on"]]
    missing = [item["id"] for item in dependencies if not item.get("commit")]
    if missing:
        raise StargateError(
            f"Task {record['id']!r} has uncommitted dependencies: {', '.join(missing)}."
        )

    initial_base = dependencies[0]["commit"] if dependencies else ctx.base_commit
    if not record.get("base_commit"):
        record["base_commit"] = initial_base
    task_ctx = _task_context(ctx, record)
    task_ctx.base_commit = initial_base
    create_worktree(task_ctx)

    checked_out = git_quiet(
        task_ctx.worktree, "symbolic-ref", "--short", "HEAD"
    ).strip()
    if checked_out != task_ctx.branch:
        raise StargateError(
            f"Task worktree {task_ctx.worktree} is on {checked_out!r}, "
            f"not {task_ctx.branch!r}."
        )
    for dependency in dependencies:
        _merge(
            task_ctx.worktree,
            dependency["branch"],
            f"preparing task {record['id']!r}",
        )
    task_ctx.base_commit = git_quiet(task_ctx.worktree, "rev-parse", "HEAD").strip()
    record["base_commit"] = task_ctx.base_commit
    record["stage"] = "ready"
    return task_ctx


def _task_prompt(overall_task: str, task: FanoutTask) -> str:
    acceptance = "\n".join(f"- {item}" for item in task.acceptance) or "- Not specified"
    dependencies = ", ".join(task.depends_on) or "none"
    return (
        f"OVERALL FAN-OUT GOAL:\n{overall_task}\n\n"
        f"ASSIGNED TASK ID: {task.id}\n"
        f"DEPENDENCIES ALREADY PRESENT: {dependencies}\n\n"
        f"TASK:\n{task.task}\n\n"
        f"ACCEPTANCE CRITERIA:\n{acceptance}"
    )


def _execute_task(
    outer: RunContext,
    task_ctx: RunContext,
    task: FanoutTask,
    prompts: list[Path],
    graph: str,
) -> dict[str, Any]:
    starting_tokens = task_ctx.tokens_used
    task_ctx.stage = "developer"
    save_state(task_ctx, "running")
    before = worktree_fingerprint(task_ctx)
    prompt = render_prompt(
        prompts,
        "developer",
        task=_task_prompt(outer.task, task),
        base_ref=task_ctx.base_commit,
        plan=graph,
    )
    print(f"\n=== TASK {task.id}: DEVELOPER ===", flush=True)
    invoke_agent(
        task_ctx,
        "developer",
        prompt,
        task_ctx.worktree,
        task_ctx.artifacts / "developer.txt",
    )
    if worktree_fingerprint(task_ctx) == before:
        raise StargateError(
            f"Developer changed nothing for fan-out task {task.id!r} in "
            f"{task_ctx.worktree}."
        )
    complete_stage(task_ctx, "developer")
    test_exit, _ = run_tests(task_ctx, "task")
    outcome = commit_run(task_ctx, "TASK_COMPLETE", test_exit)
    if outcome == "failed":
        raise StargateError(
            task_ctx.commit_error or f"Could not commit fan-out task {task.id!r}."
        )
    if not task_ctx.commit:
        raise StargateError(f"Fan-out task {task.id!r} produced no commit.")
    result = {
        "commit": task_ctx.commit,
        "test_exit": test_exit,
        "tokens_used": task_ctx.tokens_used,
        "token_delta": task_ctx.tokens_used - starting_tokens,
        "test_artifacts": sorted(task_ctx.test_artifacts),
    }
    (task_ctx.artifacts / "result.json").write_text(
        json.dumps(result, indent=2) + "\n"
    )
    save_state(task_ctx, "task_complete")
    return result


def _recover_task(ctx: RunContext, record: dict[str, Any]) -> bool:
    state_path = ctx.artifacts / "tasks" / record["id"] / "state.json"
    if not state_path.exists():
        return False
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        return False
    commit = str(state.get("commit") or "")
    if state.get("status") != "task_complete" or not commit:
        return False
    exists = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
        cwd=str(ctx.repo),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0
    if not exists:
        return False
    result_path = state_path.parent / "result.json"
    try:
        result = json.loads(result_path.read_text())
    except (OSError, ValueError):
        result = {}
    record.update(
        status="complete",
        stage="complete",
        commit=commit,
        test_exit=result.get("test_exit"),
        tokens_used=int(result.get("tokens_used", state.get("tokens_used", 0))),
        test_artifacts=list(
            result.get("test_artifacts", state.get("test_artifacts", []))
        ),
        error=None,
    )
    return True


def _run_scheduler(
    ctx: RunContext,
    tasks: list[FanoutTask],
    prompts: list[Path],
    graph: str,
    max_parallel: int,
) -> int | None:
    by_id = {task.id: task for task in tasks}
    records = ctx.fanout["tasks"]
    for task in tasks:
        record = records[task.id]
        if record["status"] == "complete":
            continue
        previous_tokens = int(record.get("tokens_used", 0))
        if _recover_task(ctx, record):
            ctx.tokens_used += max(
                0, int(record.get("tokens_used", 0)) - previous_tokens
            )
            continue
        record["status"] = "pending"
        record["stage"] = "pending"
        record["error"] = None
    save_state(ctx, "running")

    running: dict[Future[dict[str, Any]], tuple[str, RunContext]] = {}
    budget_reached = False
    executor = ThreadPoolExecutor(
        max_workers=max_parallel, thread_name_prefix="stargate-task"
    )
    try:
        while True:
            for task in tasks:
                if len(running) >= max_parallel:
                    break
                record = records[task.id]
                if record["status"] != "pending":
                    continue
                if not all(
                    records[dependency]["status"] == "complete"
                    for dependency in task.depends_on
                ):
                    continue
                if budget_spent(ctx, f"fan-out task {task.id}"):
                    budget_reached = True
                    break
                try:
                    task_ctx = _prepare_task(ctx, record)
                except StargateError as exc:
                    record.update(
                        status="failed", stage="prepare", error=str(exc)
                    )
                    save_state(ctx, "running")
                    continue
                record.update(status="running", stage="developer", error=None)
                save_state(ctx, "running")
                future = executor.submit(
                    _execute_task, ctx, task_ctx, by_id[task.id], prompts, graph
                )
                running[future] = (task.id, task_ctx)
                print(
                    f"Scheduled {task.id} on {record['branch']} "
                    f"({len(running)}/{max_parallel} slots)",
                    flush=True,
                )
            if not running:
                break

            finished, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in finished:
                task_id, task_ctx = running.pop(future)
                record = records[task_id]
                try:
                    result = future.result()
                except BaseException as exc:
                    previous_tokens = int(record.get("tokens_used", 0))
                    record.update(
                        status="failed",
                        stage=task_ctx.stage,
                        tokens_used=task_ctx.tokens_used,
                        test_artifacts=sorted(task_ctx.test_artifacts),
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    ctx.tokens_used += max(
                        0,
                        task_ctx.tokens_used - previous_tokens,
                    )
                    print(f"\nTask {task_id} failed: {exc}", file=sys.stderr)
                else:
                    record.update(
                        status="complete",
                        stage="complete",
                        commit=result["commit"],
                        test_exit=result["test_exit"],
                        tokens_used=result["tokens_used"],
                        test_artifacts=result["test_artifacts"],
                        error=None,
                    )
                    ctx.tokens_used += result["token_delta"]
                    print(
                        f"\nTask {task_id} complete at "
                        f"{result['commit'][:12]}",
                        flush=True,
                    )
                save_state(ctx, "running")
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    if budget_reached:
        save_state(ctx, "budget_exceeded")
        print(
            f"\nResume with a larger token budget: stargate resume {ctx.run_id}",
            file=sys.stderr,
        )
        return 4

    failed = [task.id for task in tasks if records[task.id]["status"] == "failed"]
    for task in tasks:
        record = records[task.id]
        if record["status"] == "pending":
            record.update(
                status="blocked",
                stage="dependencies",
                error="A dependency did not complete.",
            )
    blocked = [task.id for task in tasks if records[task.id]["status"] == "blocked"]
    if failed or blocked:
        save_state(ctx, "failed", "One or more fan-out tasks did not complete.")
        details = [*failed, *(f"{task_id} (blocked)" for task_id in blocked)]
        raise StargateError("Fan-out tasks did not complete: " + ", ".join(details))
    complete_stage(ctx, "tasks")
    return None


def _integrate(ctx: RunContext, tasks: list[FanoutTask]) -> None:
    enter_stage(ctx, "integration")
    print("\n=== INTEGRATION ===")
    create_worktree(ctx)
    integrated = ctx.fanout.setdefault("integrated", [])
    for task in tasks:
        if task.id in integrated:
            continue
        record = ctx.fanout["tasks"][task.id]
        _merge(ctx.worktree, record["branch"], f"integrating task {task.id!r}")
        integrated.append(task.id)
        ctx.commit = git_quiet(ctx.worktree, "rev-parse", "HEAD").strip()
        save_state(ctx, "running")
    complete_stage(ctx, "integration")


def _load_or_create_graph(
    ctx: RunContext,
    prompts: list[Path],
    max_tasks: int,
) -> tuple[list[FanoutTask], str]:
    graph_path = ctx.artifacts / "tasks.json"
    reusing = "architect" in ctx.done and graph_path.exists()
    if reusing:
        raw = graph_path.read_text()
        print(f"\n=== ARCHITECT (skipped, reusing {graph_path}) ===")
    else:
        enter_stage(ctx, "architect")
        architect_prompt = render_prompt(
            prompts,
            "fanout",
            task=ctx.task,
            base_ref=ctx.base_ref,
            max_tasks=str(max_tasks),
        )
        print("\n=== ARCHITECT: FAN-OUT ===")
        raw = invoke_agent(
            ctx,
            "architect",
            architect_prompt,
            ctx.repo,
            ctx.artifacts / "architect-tasks.json",
        ).strip()

    name, tasks = parse_task_graph(raw, None if reusing else max_tasks)
    graph = normalized_graph(name, tasks)
    graph_path.write_text(graph)
    _validate_frozen_graph(ctx, name, tasks)
    if not ctx.named_by_user and ctx.tag and not ctx.fanout:
        proposed = f"stargate/{short_name(name)}-{ctx.tag}"
        ctx.branch = unique_branch(ctx.repo, proposed)
        print(f"Branch:   {ctx.branch}   (named by the architect)")
    if not ctx.fanout:
        ctx.fanout = _new_task_records(ctx, name, tasks)
    if "architect" not in ctx.done:
        complete_stage(ctx, "architect")
    return tasks, graph


def orchestrate_fanout(
    args: argparse.Namespace,
    script_dir: Path,
    config: dict[str, Any],
    repo: Path,
) -> int:
    """Run or resume the opt-in DAG workflow."""
    resuming = args.command == "resume"
    if resuming:
        ctx = load_run(repo, args.run_id, config, use_frozen=args.config is None)
        prompts = [ctx.artifacts / "prompts"]
        print(f"\nResuming fan-out {ctx.run_id}: {ctx.task}")
    else:
        warn_if_dirty(repo)
        ctx = make_context(repo, config, args.task, args.base_ref, args.name)
        ctx.mode = "fanout"
        prompts = snapshot(ctx, prompt_dirs(config, script_dir))
        save_state(ctx, "running")

    try:
        if resuming and getattr(args, "redo", []):
            raise StargateError("--redo is not supported for fan-out runs.")
        if not commit_enabled(ctx.config) or args.no_commit:
            raise StargateError(
                "Fan-out requires commits to move work between task branches; "
                "remove --no-commit and enable settings.commit."
            )
        max_tasks, max_parallel = fanout_limits(
            ctx.config, args.max_parallel_tasks
        )
    except StargateError as exc:
        save_state(ctx, "failed", f"{type(exc).__name__}: {exc}")
        print(f"\nResume with: stargate resume {ctx.run_id}", file=sys.stderr)
        raise

    print(f"\nRun ID:   {ctx.run_id}")
    print(f"Mode:     fan-out ({max_parallel} parallel tasks)")
    print(f"Base:     {ctx.base_ref} @ {ctx.base_commit[:12]}")
    print(f"Branch:   {ctx.branch}")
    print(f"Worktree: {ctx.worktree}   (integration)")
    print(f"Artifacts:{ctx.artifacts}")

    try:
        plan_tests(ctx)
        tasks, graph = _load_or_create_graph(ctx, prompts, max_tasks)
        if budget_spent(ctx, "the first fan-out task"):
            save_state(ctx, "budget_exceeded")
            print(
                f"\nResume with a larger token budget: stargate resume {ctx.run_id}",
                file=sys.stderr,
            )
            return 4
        scheduler_exit = _run_scheduler(
            ctx, tasks, prompts, graph, max_parallel
        )
        if scheduler_exit is not None:
            return scheduler_exit
        _integrate(ctx, tasks)
        test_exit, test_report = run_tests(ctx, "integration")
        return review_and_finish(
            ctx,
            args,
            prompts,
            task=ctx.task,
            plan=graph,
            test_exit=test_exit,
            test_report=test_report,
            commit=True,
        )
    except (StargateError, KeyboardInterrupt) as exc:
        save_state(ctx, "failed", f"{type(exc).__name__}: {exc}")
        print(f"\nResume with: stargate resume {ctx.run_id}", file=sys.stderr)
        raise
