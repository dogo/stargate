"""Focused regression tests for fan-out graph validation and freezing."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import stargate.fanout as fanout
from stargate.core import RunContext, StargateError
from tests.harness import make_repo, sh


def _task(task_id: str, **changes: object) -> dict[str, object]:
    task: dict[str, object] = {
        "id": task_id,
        "task": f"Implement {task_id}.",
        "depends_on": [],
        "acceptance": [f"{task_id} works"],
    }
    task.update(changes)
    return task


def _graph(tasks: list[object], name: object = "graph audit") -> str:
    return json.dumps({"name": name, "tasks": tasks})


def _parse_error(raw: str, max_tasks: int = 8) -> str:
    try:
        fanout.parse_task_graph(raw, max_tasks)
    except StargateError as exc:
        return str(exc)
    raise AssertionError("graph unexpectedly passed validation")


def _context(root: Path, repo: Path) -> RunContext:
    artifacts = root / "artifacts"
    artifacts.mkdir()
    return RunContext(
        repo=repo,
        config={},
        run_id="20260902-graph-audit",
        slug="graph-audit",
        branch="stargate/original-task-20260902-120000",
        base_ref="main",
        base_commit="base",
        worktree=root / "integration",
        artifacts=artifacts,
        task="Audit the graph",
        tag="20260902-120000",
        mode="fanout",
    )


def test_fanout_settings_require_actual_positive_integers(root: Path) -> None:
    del root
    for key in ("max_fanout_tasks", "max_parallel_tasks"):
        expected = f"settings.{key} must be a positive integer."
        for value in (True, False, 1.5, "2", 0, -1):
            try:
                fanout.fanout_limits({"settings": {key: value}})
            except StargateError as exc:
                assert str(exc) == expected, (key, value, str(exc))
            else:
                raise AssertionError(f"settings.{key} accepted {value!r}")


def test_parallel_override_requires_an_actual_positive_integer(root: Path) -> None:
    del root
    for value in (True, 1.5, "2", 0):
        try:
            fanout.fanout_limits({}, value)  # type: ignore[arg-type]
        except StargateError as exc:
            assert str(exc) == "--max-parallel-tasks must be a positive integer."
        else:
            raise AssertionError(f"parallel override accepted {value!r}")


def test_parallel_limit_is_not_clamped_to_the_graph_size_ceiling(root: Path) -> None:
    del root
    limits = fanout.fanout_limits(
        {"settings": {"max_fanout_tasks": 2, "max_parallel_tasks": 7}}
    )
    assert limits == (2, 7)


def test_malformed_graph_json_reports_its_location(root: Path) -> None:
    del root
    error = _parse_error('{"name": "broken", "tasks": [}')
    assert "did not return valid JSON" in error
    assert "line 1, column" in error


def test_non_object_task_reports_its_index(root: Path) -> None:
    del root
    error = _parse_error(_graph([_task("alpha"), "not a task"]))
    assert error == "tasks[1] must be an object."


def test_bad_task_id_reports_the_value_and_index(root: Path) -> None:
    del root
    error = _parse_error(_graph([_task("Bad ID")]))
    assert "tasks[0].id ('Bad ID')" in error
    assert "[a-z0-9][a-z0-9-]{0,47}" in error


def test_duplicate_task_id_reports_the_value_and_index(root: Path) -> None:
    del root
    error = _parse_error(_graph([_task("alpha"), _task("alpha")]))
    assert error == "tasks[1].id duplicates fan-out task 'alpha'."


def test_unknown_dependency_reports_the_dependent_task(root: Path) -> None:
    del root
    error = _parse_error(_graph([_task("alpha", depends_on=["missing"])]))
    assert error == "Fan-out task 'alpha' depends on unknown task(s): missing."


def test_duplicate_dependency_reports_both_task_ids(root: Path) -> None:
    del root
    error = _parse_error(
        _graph(
            [
                _task("base"),
                _task("alpha", depends_on=["base", "base"]),
            ]
        )
    )
    assert "Fan-out task 'alpha' at tasks[1]" in error
    assert "duplicate task ID 'base'" in error


def test_self_dependency_reports_the_task_id(root: Path) -> None:
    del root
    error = _parse_error(_graph([_task("alpha", depends_on=["alpha"])]))
    assert error == "Fan-out task 'alpha' depends on itself."


def test_cycle_reports_only_tasks_on_the_cycle(root: Path) -> None:
    del root
    error = _parse_error(
        _graph(
            [
                _task("alpha", depends_on=["beta"]),
                _task("beta", depends_on=["alpha"]),
                _task("downstream", depends_on=["beta"]),
            ]
        )
    )
    assert error == (
        "Fan-out task dependencies contain a cycle: alpha -> beta -> alpha."
    )


def test_oversized_graph_reports_the_first_excess_task(root: Path) -> None:
    del root
    error = _parse_error(
        _graph([_task("alpha"), _task("beta"), _task("gamma")]),
        max_tasks=2,
    )
    assert "settings.max_fanout_tasks is 2" in error
    assert "tasks[2] ('gamma')" in error


def test_empty_graph_name_is_rejected(root: Path) -> None:
    del root
    for name in (None, "", "   ", "---"):
        error = _parse_error(_graph([_task("alpha")], name=name))
        assert error == "Fan-out architect output needs a usable string 'name'."


def test_acceptance_is_optional_but_must_be_non_empty_when_present(
    root: Path,
) -> None:
    del root
    optional = _task("optional")
    optional.pop("acceptance")
    name, tasks = fanout.parse_task_graph(_graph([optional]), 8)
    assert tasks[0].acceptance == ()
    normalized = fanout.normalized_graph(name, tasks)
    assert "acceptance" not in normalized
    assert fanout.parse_task_graph(normalized, 8) == (name, tasks)

    for value in ([], [""], ["   "], "done", [1]):
        error = _parse_error(_graph([_task("alpha", acceptance=value)]))
        assert "Fan-out task 'alpha' at tasks[0].acceptance" in error
        assert "non-empty list of non-empty strings" in error


def test_frozen_graph_detects_sequence_type_and_order_changes(root: Path) -> None:
    repo = make_repo(root)
    ctx = _context(root, repo)
    _, tasks = fanout.parse_task_graph(
        _graph(
            [
                _task("alpha"),
                _task("beta"),
                _task("final", depends_on=["alpha", "beta"]),
            ]
        ),
        8,
    )
    ctx.fanout = fanout._new_task_records(ctx, "graph audit", tasks)

    ctx.fanout["tasks"]["alpha"]["acceptance"] = ("alpha works",)
    try:
        fanout._validate_frozen_graph(ctx, "graph audit", tasks)
    except StargateError as exc:
        assert "no longer matches" in str(exc)
    else:
        raise AssertionError("tuple mutation was not detected")

    ctx.fanout["tasks"]["alpha"]["acceptance"] = ["alpha works"]
    ctx.fanout["tasks"]["final"]["depends_on"] = ["beta", "alpha"]
    try:
        fanout._validate_frozen_graph(ctx, "graph audit", tasks)
    except StargateError as exc:
        assert "no longer matches" in str(exc)
    else:
        raise AssertionError("dependency ordering mutation was not detected")


def test_graph_mismatch_does_not_overwrite_the_frozen_artifact(root: Path) -> None:
    repo = make_repo(root)
    ctx = _context(root, repo)
    name, tasks = fanout.parse_task_graph(_graph([_task("alpha")]), 8)
    frozen = fanout.normalized_graph(name, tasks).encode()
    graph_path = ctx.artifacts / "tasks.json"
    graph_path.write_bytes(frozen)
    ctx.fanout = fanout._new_task_records(ctx, name, tasks)
    ctx.stage = "architect"
    changed = _graph([_task("alpha", task="Changed after freezing.")])

    with (
        patch("stargate.fanout.render_prompt", return_value="architect prompt"),
        patch("stargate.fanout.invoke_agent", return_value=changed),
    ):
        try:
            fanout._load_or_create_graph(ctx, [], 8)
        except StargateError as exc:
            assert str(exc) == (
                "tasks.json no longer matches the fan-out graph frozen in "
                "state.json."
            )
        else:
            raise AssertionError("changed architect graph was accepted")

    assert graph_path.read_bytes() == frozen


def test_completed_architect_restores_missing_graph_without_rerunning(
    root: Path,
) -> None:
    repo = make_repo(root)
    ctx = _context(root, repo)
    name, expected_tasks = fanout.parse_task_graph(_graph([_task("alpha")]), 8)
    ctx.fanout = fanout._new_task_records(ctx, name, expected_tasks)
    ctx.done.add("architect")
    ctx.stage = "tasks"

    with patch(
        "stargate.fanout.invoke_agent",
        side_effect=AssertionError("architect unexpectedly re-ran"),
    ):
        tasks, graph = fanout._load_or_create_graph(ctx, [], 8)

    assert tasks == expected_tasks
    assert (ctx.artifacts / "tasks.json").read_text() == graph
    assert ctx.stage == "tasks"


def test_reused_graph_still_obeys_the_current_task_limit(root: Path) -> None:
    repo = make_repo(root)
    ctx = _context(root, repo)
    name, frozen_tasks = fanout.parse_task_graph(_graph([_task("alpha")]), 8)
    ctx.fanout = fanout._new_task_records(ctx, name, frozen_tasks)
    ctx.done.add("architect")
    edited = _graph([_task("alpha"), _task("excess")]).encode()
    graph_path = ctx.artifacts / "tasks.json"
    graph_path.write_bytes(edited)

    error = ""
    with patch(
        "stargate.fanout.invoke_agent",
        side_effect=AssertionError("architect unexpectedly re-ran"),
    ):
        try:
            fanout._load_or_create_graph(ctx, [], 1)
        except StargateError as exc:
            error = str(exc)

    assert "settings.max_fanout_tasks is 1" in error
    assert "tasks[1] ('excess')" in error
    assert graph_path.read_bytes() == edited


def test_architect_branch_is_saved_before_task_branches_are_derived(
    root: Path,
) -> None:
    repo = make_repo(root)
    ctx = _context(root, repo)
    events: list[tuple[str, str]] = []

    def record_unique(_repo: Path, proposed: str) -> str:
        events.append(("branch", proposed))
        return proposed

    def record_save(saved: RunContext, _status: str) -> None:
        events.append(("save", saved.branch))

    with (
        patch("stargate.fanout.render_prompt", return_value="architect prompt"),
        patch(
            "stargate.fanout.invoke_agent",
            return_value=_graph([_task("alpha")]),
        ),
        patch("stargate.fanout.unique_branch", side_effect=record_unique),
        patch("stargate.fanout.save_state", side_effect=record_save),
        patch("stargate.fanout.enter_stage"),
        patch("stargate.fanout.complete_stage"),
    ):
        fanout._load_or_create_graph(ctx, [], 8)

    proposed = "stargate/graph-audit-20260902-120000"
    task_branch = f"{proposed}-alpha"
    assert events == [
        ("branch", proposed),
        ("save", proposed),
        ("branch", task_branch),
    ]
    assert ctx.branch == proposed


def test_task_record_branches_do_not_collide_with_task_id_suffixes(
    root: Path,
) -> None:
    repo = make_repo(root)
    ctx = _context(root, repo)
    sh(f"git branch {ctx.branch}-alpha", repo)
    _, tasks = fanout.parse_task_graph(
        _graph([_task("alpha"), _task("alpha-2")]), 8
    )

    records = fanout._new_task_records(ctx, "graph audit", tasks)["tasks"]

    assert records["alpha"]["branch"] == f"{ctx.branch}-alpha-2"
    assert records["alpha-2"]["branch"] == f"{ctx.branch}-alpha-2-2"
    assert len({record["branch"] for record in records.values()}) == 2
