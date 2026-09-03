"""Focused regressions for fan-out scheduling, recovery, and integration."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import yaml

import stargate.fanout as fanout
from stargate.core import RunContext, StargateError
from tests.harness import ROOT, git_output, make_repo, run, sh, write_fanout_config


def _tasks(*items: tuple[str, tuple[str, ...]]) -> list[fanout.FanoutTask]:
    return [
        fanout.FanoutTask(task_id, f"Implement {task_id}.", dependencies, ())
        for task_id, dependencies in items
    ]


def _context(root: Path, repo: Path, tasks: list[fanout.FanoutTask]) -> RunContext:
    artifacts = repo / ".stargate" / "runs" / "scheduler-test"
    artifacts.mkdir(parents=True)
    base_commit = git_output(repo, "rev-parse", "HEAD")
    ctx = RunContext(
        repo=repo,
        config={"settings": {"test_command": "true"}},
        run_id="scheduler-test",
        slug="scheduler-test",
        branch="stargate/scheduler-test",
        base_ref="main",
        base_commit=base_commit,
        worktree=root / "worktrees" / "scheduler-test",
        artifacts=artifacts,
        task="Audit the scheduler",
        mode="fanout",
    )
    ctx.fanout = fanout._new_task_records(ctx, "scheduler test", tasks)
    return ctx


def _commit_task(
    ctx: RunContext,
    task_id: str,
    filename: str,
    contents: str,
) -> str:
    record = ctx.fanout["tasks"][task_id]
    worktree = Path(record["worktree"])
    worktree.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "git",
            "worktree",
            "add",
            "-q",
            "-b",
            record["branch"],
            str(worktree),
            ctx.base_commit,
        ],
        cwd=ctx.repo,
        check=True,
    )
    (worktree / filename).write_text(contents)
    sh(f"git add {filename} && git commit -qm {task_id}", worktree)
    commit = git_output(worktree, "rev-parse", "HEAD")
    record.update(
        status="complete",
        stage="complete",
        base_commit=ctx.base_commit,
        commit=commit,
    )
    return commit


def _resume(repo: Path, config: Path, run_id: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "stargate",
            "--config",
            str(config),
            "resume",
            run_id,
        ],
        cwd=repo,
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )


def test_dependent_task_base_contains_every_dependency(root: Path) -> None:
    repo = make_repo(root)
    tasks = _tasks(
        ("alpha", ()),
        ("beta", ()),
        ("combined", ("alpha", "beta")),
    )
    ctx = _context(root, repo, tasks)
    alpha = _commit_task(ctx, "alpha", "alpha.txt", "alpha\n")
    beta = _commit_task(ctx, "beta", "beta.txt", "beta\n")

    task_ctx = fanout._prepare_task(ctx, ctx.fanout["tasks"]["combined"])

    assert task_ctx.base_commit == git_output(task_ctx.worktree, "rev-parse", "HEAD")
    assert ctx.fanout["tasks"]["combined"]["base_commit"] == task_ctx.base_commit
    for dependency in (alpha, beta):
        assert subprocess.run(
            ["git", "merge-base", "--is-ancestor", dependency, task_ctx.base_commit],
            cwd=repo,
        ).returncode == 0
    assert (task_ctx.worktree / "alpha.txt").read_text() == "alpha\n"
    assert (task_ctx.worktree / "beta.txt").read_text() == "beta\n"


def test_failed_task_retry_discards_partial_edits_at_the_frozen_base(
    root: Path,
) -> None:
    repo = make_repo(root)
    ctx = _context(root, repo, _tasks(("alpha", ())))
    record = ctx.fanout["tasks"]["alpha"]
    task_ctx = fanout._prepare_task(ctx, record)
    frozen_base = record["base_commit"]
    (task_ctx.worktree / "app.py").write_text("partial tracked edit\n")
    (task_ctx.worktree / "partial.txt").write_text("partial untracked edit\n")

    output = StringIO()
    with redirect_stdout(output):
        retried = fanout._prepare_task(ctx, record)

    assert "Clean retry for task alpha" in output.getvalue()
    assert "discarding the unfinished attempt's edits" in output.getvalue()
    assert retried.base_commit == frozen_base
    assert git_output(retried.worktree, "rev-parse", "HEAD") == frozen_base
    assert (retried.worktree / "app.py").read_text() == "x = 1\n"
    assert not (retried.worktree / "partial.txt").exists()
    assert git_output(retried.worktree, "status", "--porcelain") == ""


def test_scheduler_persists_tasks_as_the_live_parallel_stage(root: Path) -> None:
    repo = make_repo(root)
    tasks = _tasks(("alpha", ()))
    ctx = _context(root, repo, tasks)
    seen: dict[str, object] = {}

    def execute(
        _outer: RunContext,
        task_ctx: RunContext,
        _task: fanout.FanoutTask,
        _prompts: list[Path],
        _graph: str,
    ) -> dict[str, object]:
        state = json.loads((ctx.artifacts / "state.json").read_text())
        seen.update(stage=state["stage"], status=state["status"])
        return {
            "commit": git_output(task_ctx.worktree, "rev-parse", "HEAD"),
            "test_exit": 0,
            "tokens_used": 0,
            "test_artifacts": [],
        }

    with patch("stargate.fanout._execute_task", side_effect=execute):
        assert fanout._run_scheduler(ctx, tasks, [], "{}", 1) is None

    state = json.loads((ctx.artifacts / "state.json").read_text())
    assert seen == {"stage": "tasks", "status": "running"}
    assert state["stage"] == "tasks"
    assert "tasks" in state["completed"]


def test_committed_task_is_recovered_without_a_second_commit_or_token_charge(
    root: Path,
) -> None:
    repo = make_repo(root)
    tasks = _tasks(("alpha", ()))
    ctx = _context(root, repo, tasks)
    record = ctx.fanout["tasks"]["alpha"]
    task_ctx = fanout._prepare_task(ctx, record)
    original_save = fanout.save_state

    def implement(saved: RunContext, *_args: object) -> str:
        (saved.worktree / "alpha.txt").write_text("done\n")
        saved.tokens_used += 11
        return "done"

    def crash_before_completion(
        saved: RunContext, status: str, error: str | None = None
    ) -> None:
        if status == "task_complete":
            raise RuntimeError("simulated crash after commit")
        original_save(saved, status, error)

    with (
        patch("stargate.fanout.render_prompt", return_value="prompt"),
        patch("stargate.fanout.invoke_agent", side_effect=implement),
        patch("stargate.fanout.run_tests", return_value=(0, "passed")),
        patch("stargate.fanout.save_state", side_effect=crash_before_completion),
    ):
        try:
            fanout._execute_task(ctx, task_ctx, tasks[0], [], "{}")
        except RuntimeError as exc:
            assert str(exc) == "simulated crash after commit"
        else:
            raise AssertionError("simulated post-commit crash did not happen")

    task_state = json.loads(
        (task_ctx.artifacts / "state.json").read_text()
    )
    assert task_state["status"] == "running"
    assert task_state["commit"] is None
    terminal_commit = git_output(task_ctx.worktree, "rev-parse", "HEAD")
    assert git_output(
        repo, "rev-list", "--count", f"{ctx.base_commit}..{record['branch']}"
    ) == "1"

    with patch(
        "stargate.fanout._execute_task",
        side_effect=AssertionError("completed task unexpectedly re-ran"),
    ):
        assert fanout._run_scheduler(ctx, tasks, [], "{}", 1) is None
        assert fanout._run_scheduler(ctx, tasks, [], "{}", 1) is None

    assert record["commit"] == terminal_commit
    assert record["tokens_used"] == 11
    assert ctx.tokens_used == 11
    assert git_output(
        repo, "rev-list", "--count", f"{ctx.base_commit}..{record['branch']}"
    ) == "1"


def test_merge_conflict_falls_back_to_reset_when_abort_fails(root: Path) -> None:
    repo = make_repo(root)
    integration = root / "integration"
    other = root / "other"
    base = git_output(repo, "rev-parse", "HEAD")
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "integration", str(integration), base],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "other", str(other), base],
        cwd=repo,
        check=True,
    )
    (integration / "app.py").write_text("integration\n")
    sh("git add app.py && git commit -qm integration", integration)
    (other / "app.py").write_text("other\n")
    sh("git add app.py && git commit -qm other", other)
    real_run = subprocess.run

    def fail_abort(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[:3] == ["git", "merge", "--abort"]:
            return subprocess.CompletedProcess(args, 1, "simulated abort failure")
        return real_run(args, **kwargs)

    with patch("stargate.fanout.subprocess.run", side_effect=fail_abort):
        try:
            fanout._merge(integration, "other", "testing conflict cleanup")
        except StargateError as exc:
            assert "Could not merge 'other' while testing conflict cleanup" in str(exc)
        else:
            raise AssertionError("conflicting merge unexpectedly succeeded")

    assert subprocess.run(
        ["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"],
        cwd=integration,
        capture_output=True,
    ).returncode != 0
    assert git_output(integration, "ls-files", "-u") == ""
    assert git_output(integration, "status", "--porcelain") == ""

    sh("git reset -q --hard integration && echo resumed > resumed.txt && "
       "git add resumed.txt && git commit -qm resumed", other)
    fanout._merge(integration, "other", "resuming integration")
    assert (integration / "resumed.txt").read_text() == "resumed\n"


def test_integration_rejects_a_worktree_on_the_wrong_branch(root: Path) -> None:
    repo = make_repo(root)
    ctx = _context(root, repo, [])
    ctx.worktree.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "git",
            "worktree",
            "add",
            "-q",
            "-b",
            "wrong-integration",
            str(ctx.worktree),
            ctx.base_commit,
        ],
        cwd=repo,
        check=True,
    )

    # Isolate the integration-specific assertion even if create_worktree grows
    # its own generic validation.
    with patch("stargate.fanout.create_worktree"):
        try:
            fanout._integrate(ctx, [])
        except StargateError as exc:
            assert "Integration worktree" in str(exc)
            assert "not 'stargate/scheduler-test'" in str(exc)
        else:
            raise AssertionError("wrong integration branch was accepted")


def test_parallel_budget_stop_is_bounded_reported_and_resumable(root: Path) -> None:
    repo = make_repo(root)
    graph = root / "tasks.json"
    graph.write_text(
        json.dumps(
            {
                "name": "bounded budget",
                "tasks": [
                    {"id": task_id, "task": f"write {task_id}", "depends_on": []}
                    for task_id in ("alpha", "beta", "gamma")
                ],
            }
        )
    )
    calls = root / "calls.txt"
    alpha_started = root / "alpha-started"
    beta_started = root / "beta-started"
    developer = (
        'case "$0" in '
        '*"TASK ID: alpha"*) id=alpha ;; '
        '*"TASK ID: beta"*) id=beta ;; '
        '*"TASK ID: gamma"*) id=gamma ;; '
        '*) exit 19 ;; esac; '
        f'echo "$id" >> {calls}; '
        f'if test "$id" = alpha; then touch {alpha_started}; '
        f'while test ! -e {beta_started}; do sleep 0.01; done; fi; '
        f'if test "$id" = beta; then touch {beta_started}; '
        f'while test ! -e {alpha_started}; do sleep 0.01; done; fi; '
        'echo "$id" > "$id.txt"; echo "tokens used 6"'
    )
    config = root / "fanout.yaml"
    write_fanout_config(config, graph, developer, max_parallel=2)
    data = yaml.safe_load(config.read_text())
    data["agents"]["dev"]["usage_pattern"] = r"tokens used (\d+)"
    data["settings"]["max_task_tokens"] = 5
    config.write_text(yaml.safe_dump(data))

    first = run(repo, config, "bounded fanout", "--fan-out")

    assert first.returncode == 4, first.stdout + first.stderr
    assert sorted(calls.read_text().splitlines()) == ["alpha", "beta"]
    assert "Up to 2 concurrently started task agent(s)" in first.stderr
    assert "=== RESULT ===" in first.stdout
    assert "Verdict:   BUDGET_EXCEEDED" in first.stdout
    state_path = next((repo / ".stargate" / "runs").glob("*/state.json"))
    state = json.loads(state_path.read_text())
    assert state["status"] == "budget_exceeded"
    assert state["stage"] == "tasks"
    assert state["tokens_used"] == 12
    assert (state_path.parent / "summary.md").exists()
    assert "Verdict: BUDGET_EXCEEDED" in (
        state_path.parent / "summary.md"
    ).read_text()

    data["settings"]["max_task_tokens"] = 100
    config.write_text(yaml.safe_dump(data))
    resumed = _resume(repo, config, state["run_id"])

    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert sorted(calls.read_text().splitlines()) == ["alpha", "beta", "gamma"]
    state = json.loads(state_path.read_text())
    assert state["status"] == "approved"
    assert state["tokens_used"] == 18
    assert all(
        record["status"] == "complete"
        for record in state["fanout"]["tasks"].values()
    )


def test_interrupt_settles_running_records_for_a_clean_resume(root: Path) -> None:
    repo = make_repo(root)
    tasks = _tasks(("alpha", ()))
    ctx = _context(root, repo, tasks)
    started = threading.Event()
    release = threading.Event()

    def interrupted_execute(
        _outer: RunContext,
        task_ctx: RunContext,
        *_args: object,
    ) -> dict[str, object]:
        started.set()
        release.wait(timeout=5)
        task_ctx.tokens_used += 4
        raise StargateError("agent terminated")

    def interrupt_wait(*_args: object, **_kwargs: object) -> None:
        assert started.wait(timeout=5)
        release.set()
        raise KeyboardInterrupt()

    with (
        patch("stargate.fanout._execute_task", side_effect=interrupted_execute),
        patch("stargate.fanout.wait", side_effect=interrupt_wait),
    ):
        try:
            fanout._run_scheduler(ctx, tasks, [], "{}", 1)
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError("scheduler interrupt was swallowed")

    state = json.loads((ctx.artifacts / "state.json").read_text())
    record = state["fanout"]["tasks"]["alpha"]
    assert state["status"] == "failed"
    assert state["stage"] == "tasks"
    assert record["status"] == "pending"
    assert record["stage"] == "interrupted"
    assert record["tokens_used"] == 4
    assert state["tokens_used"] == 4

    def resumed_execute(
        _outer: RunContext,
        task_ctx: RunContext,
        *_args: object,
    ) -> dict[str, object]:
        return {
            "commit": git_output(task_ctx.worktree, "rev-parse", "HEAD"),
            "test_exit": 0,
            "tokens_used": task_ctx.tokens_used + 1,
            "test_artifacts": [],
        }

    with patch("stargate.fanout._execute_task", side_effect=resumed_execute):
        assert fanout._run_scheduler(ctx, tasks, [], "{}", 1) is None

    assert ctx.tokens_used == 5
    assert ctx.fanout["tasks"]["alpha"]["status"] == "complete"


def test_invalid_fanout_commit_modes_leave_no_run_artifacts(root: Path) -> None:
    repo = make_repo(root)
    graph = root / "tasks.json"
    graph.write_text(
        json.dumps(
            {
                "name": "preflight",
                "tasks": [{"id": "alpha", "task": "alpha", "depends_on": []}],
            }
        )
    )
    config = root / "fanout.yaml"
    write_fanout_config(config, graph, "echo alpha > alpha.txt")

    no_commit = run(repo, config, "invalid fanout", "--fan-out", "--no-commit")
    assert no_commit.returncode == 1
    assert "commit" in no_commit.stderr.lower()
    assert not (repo / ".stargate").exists()

    data = yaml.safe_load(config.read_text())
    data["settings"]["commit"] = False
    config.write_text(yaml.safe_dump(data))
    disabled = run(repo, config, "invalid fanout", "--fan-out")
    assert disabled.returncode == 1
    assert "commit" in disabled.stderr.lower()
    assert not (repo / ".stargate").exists()


def test_unsupported_resume_redo_does_not_load_or_mutate_the_run(root: Path) -> None:
    del root
    args = type(
        "Args",
        (),
        {
            "command": "resume",
            "redo": ["developer"],
            "no_commit": False,
        },
    )()
    with patch(
        "stargate.fanout.load_run",
        side_effect=AssertionError("unsupported resume unexpectedly loaded state"),
    ):
        try:
            fanout.orchestrate_fanout(args, Path("."), {}, Path("."))
        except StargateError as exc:
            assert str(exc) == "--redo is not supported for fan-out runs."
        else:
            raise AssertionError("unsupported fan-out redo was accepted")


def test_parallel_git_worktree_operations_remain_isolated_under_repetition(
    root: Path,
) -> None:
    repo = make_repo(root)
    task_ids = tuple(f"task-{index}" for index in range(6))
    graph = root / "tasks.json"
    graph.write_text(
        json.dumps(
            {
                "name": "git concurrency",
                "tasks": [
                    {"id": task_id, "task": task_id, "depends_on": []}
                    for task_id in task_ids
                ],
            }
        )
    )
    cases = " ".join(
        f'*"TASK ID: {task_id}"*) echo {task_id} > {task_id}.txt ;;'
        for task_id in task_ids
    )
    config = root / "concurrent.yaml"
    write_fanout_config(
        config,
        graph,
        f'case "$0" in {cases} *) exit 23 ;; esac; sleep 0.05',
        max_parallel=len(task_ids),
    )

    for _ in range(3):
        result = run(repo, config, "repeat git concurrency", "--fan-out")
        assert result.returncode == 0, result.stdout + result.stderr

    states = sorted((repo / ".stargate" / "runs").glob("*/state.json"))
    assert len(states) == 3
    for state_path in states:
        state = json.loads(state_path.read_text())
        records = state["fanout"]["tasks"]
        assert len({record["branch"] for record in records.values()}) == len(task_ids)
        assert len({record["worktree"] for record in records.values()}) == len(task_ids)
        assert all(record["status"] == "complete" for record in records.values())
        integration = Path(state["worktree"])
        assert all((integration / f"{task_id}.txt").exists() for task_id in task_ids)
    subprocess.run(
        ["git", "fsck", "--full"],
        cwd=repo,
        check=True,
        stdout=subprocess.DEVNULL,
    )
