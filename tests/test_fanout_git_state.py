"""Regression tests for fan-out Git worktrees, state writes, and cleanup."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import stargate.run as run_module
from stargate.core import RunContext, StargateError
from stargate.run import create_worktree, save_state, unique_branch
from tests.harness import (
    clean,
    git_output,
    make_repo,
    recorded_branch_exists,
    recorded_run,
    runs,
)


def _context(repo: Path, worktree: Path, branch: str, artifacts: Path) -> RunContext:
    artifacts.mkdir(parents=True, exist_ok=True)
    return RunContext(
        repo=repo,
        config={},
        run_id="fanout-state",
        slug="fanout-state",
        branch=branch,
        base_ref="main",
        base_commit=git_output(repo, "rev-parse", "main"),
        worktree=worktree,
        artifacts=artifacts,
        task="fan-out state audit",
        mode="fanout",
    )


def _add_worktree(repo: Path, path: Path, branch: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", branch, str(path), "main"],
        cwd=repo,
        check=True,
    )


def _commit_file(worktree: Path, name: str, contents: str) -> None:
    (worktree / name).write_text(contents)
    subprocess.run(["git", "add", name], cwd=worktree, check=True)
    subprocess.run(
        ["git", "commit", "-qm", f"add {name}"], cwd=worktree, check=True
    )


def _write_fanout_state(
    repo: Path,
    run_id: str,
    branch: str,
    integration: Path,
    tasks: dict[str, tuple[str, Path]],
) -> Path:
    artifacts = repo / ".stargate" / "runs" / run_id
    artifacts.mkdir(parents=True)
    state = {
        "run_id": run_id,
        "repo": str(repo),
        "branch": branch,
        "worktree": str(integration),
        "mode": "fanout",
        "fanout": {
            "name": "cleanup graph",
            "order": list(tasks),
            "tasks": {
                task_id: {"branch": task_branch, "worktree": str(task_worktree)}
                for task_id, (task_branch, task_worktree) in tasks.items()
            },
        },
    }
    (artifacts / "state.json").write_text(json.dumps(state))
    return artifacts


def _fanout_run(
    root: Path,
) -> tuple[Path, str, str, Path, dict[str, tuple[str, Path]], Path]:
    repo = make_repo(root)
    run_id = "fanout-clean"
    branch = "stargate/fanout-clean"
    worktree_root = root / "worktrees"
    integration = worktree_root / run_id
    _add_worktree(repo, integration, branch)

    tasks: dict[str, tuple[str, Path]] = {}
    for task_id in ("alpha", "beta"):
        task_branch = f"{branch}-{task_id}"
        task_worktree = worktree_root / f"{run_id}-{task_id}"
        _add_worktree(repo, task_worktree, task_branch)
        _commit_file(task_worktree, f"{task_id}.txt", f"{task_id}\n")
        subprocess.run(
            ["git", "merge", "-q", "--no-edit", task_branch],
            cwd=integration,
            check=True,
        )
        tasks[task_id] = (task_branch, task_worktree)

    subprocess.run(
        ["git", "merge", "-q", "--ff-only", branch], cwd=repo, check=True
    )
    artifacts = _write_fanout_state(
        repo, run_id, branch, integration, tasks
    )
    return repo, run_id, branch, integration, tasks, artifacts


def _raises_stargate_error(call: Callable[[], object]) -> str:
    try:
        call()
    except StargateError as exc:
        return str(exc)
    raise AssertionError("expected StargateError")


def test_create_worktree_rejects_an_unregistered_existing_path(root: Path) -> None:
    repo = make_repo(root)
    worktree = root / "worktrees" / "fanout-state-alpha"
    worktree.mkdir(parents=True)
    ctx = _context(
        repo,
        worktree,
        "stargate/fanout-state-alpha",
        root / "artifacts" / "alpha",
    )

    error = _raises_stargate_error(lambda: create_worktree(ctx))

    assert "is not a registered Git worktree" in error, error
    assert worktree.exists()
    assert not recorded_branch_exists(repo, ctx.branch)


def test_create_worktree_rejects_a_registered_path_on_the_wrong_branch(
    root: Path,
) -> None:
    repo = make_repo(root)
    worktree = root / "worktrees" / "fanout-state"
    _add_worktree(repo, worktree, "stargate/unrelated")
    ctx = _context(
        repo,
        worktree,
        "stargate/fanout-state",
        root / "artifacts" / "integration",
    )

    error = _raises_stargate_error(lambda: create_worktree(ctx))

    assert "registered on branch 'stargate/unrelated'" in error, error
    assert "not expected branch 'stargate/fanout-state'" in error, error
    assert git_output(worktree, "branch", "--show-current") == "stargate/unrelated"


def test_create_worktree_refuses_a_branch_taken_after_name_selection(
    root: Path,
) -> None:
    repo = make_repo(root)
    branch = unique_branch(repo, "stargate/raced-task")
    subprocess.run(["git", "branch", branch, "main"], cwd=repo, check=True)
    worktree = root / "worktrees" / "raced-task"
    ctx = _context(repo, worktree, branch, root / "artifacts" / "raced-task")

    error = _raises_stargate_error(lambda: create_worktree(ctx))

    assert "already exists without the expected registered worktree" in error, error
    assert not worktree.exists()
    assert git_output(repo, "rev-parse", branch) == git_output(repo, "rev-parse", "main")


def test_create_worktree_keeps_linear_missing_worktree_recovery(root: Path) -> None:
    repo = make_repo(root)
    branch = "stargate/linear-recovery"
    subprocess.run(["git", "branch", branch, "main"], cwd=repo, check=True)
    worktree = root / "worktrees" / "linear-recovery"
    ctx = _context(repo, worktree, branch, root / "artifacts" / "linear-recovery")
    ctx.mode = "linear"

    create_worktree(ctx)

    assert worktree.exists()
    assert git_output(worktree, "branch", "--show-current") == branch


def test_save_state_replaces_atomically_when_the_final_replace_fails(
    root: Path,
) -> None:
    repo = make_repo(root)
    ctx = _context(
        repo,
        root / "worktrees" / "state",
        "stargate/state",
        root / "artifacts" / "state",
    )
    save_state(ctx, "running")
    state_path = ctx.artifacts / "state.json"
    original = state_path.read_bytes()
    original_replace = run_module.os.replace

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("simulated interruption before replace")

    run_module.os.replace = fail_replace
    try:
        try:
            save_state(ctx, "failed", "new state must not truncate the old one")
        except OSError as exc:
            assert "simulated interruption" in str(exc), exc
        else:
            raise AssertionError("failed replace unexpectedly saved state")
    finally:
        run_module.os.replace = original_replace

    assert state_path.read_bytes() == original
    assert not list(ctx.artifacts.glob(".state.json.*.tmp"))


def test_list_marks_a_linear_budget_stop_resumable(root: Path) -> None:
    repo = make_repo(root)
    run_id = "linear-budget-stop"
    artifacts = repo / ".stargate" / "runs" / run_id
    artifacts.mkdir(parents=True)
    (artifacts / "state.json").write_text(json.dumps({
        "run_id": run_id,
        "status": "budget_exceeded",
        "stage": "developer",
        "mode": "linear",
        "branch": "stargate/linear-budget-stop",
        "worktree": str(root / "missing-worktree"),
        "task": "linear task",
    }))

    proc = runs(repo)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    line = next(line for line in proc.stdout.splitlines() if run_id in line)
    assert line.startswith("* "), line
    assert f"stargate resume {run_id}" in proc.stdout, proc.stdout


def test_clean_fanout_removes_every_recorded_target(root: Path) -> None:
    repo, run_id, branch, integration, tasks, artifacts = _fanout_run(root)

    proc = clean(repo, run_id)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not artifacts.exists() and not integration.exists()
    assert not recorded_branch_exists(repo, branch)
    assert all(
        not path.exists() and not recorded_branch_exists(repo, task_branch)
        for task_branch, path in tasks.values()
    )


def test_clean_fanout_removes_only_its_stale_worktree_registration(
    root: Path,
) -> None:
    repo, run_id, branch, integration, tasks, artifacts = _fanout_run(root)
    alpha_branch, alpha_worktree = tasks["alpha"]
    unrelated = root / "worktrees" / "temporarily-missing-user-worktree"
    _add_worktree(repo, unrelated, "user/temporarily-missing")
    shutil.rmtree(unrelated)
    shutil.rmtree(alpha_worktree)

    proc = clean(repo, "--all")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not artifacts.exists() and not integration.exists()
    assert not recorded_branch_exists(repo, branch)
    assert not recorded_branch_exists(repo, alpha_branch)
    assert all(
        not path.exists() and not recorded_branch_exists(repo, task_branch)
        for task_branch, path in tasks.values()
    )
    registrations = git_output(repo, "worktree", "list", "--porcelain")
    assert f"worktree {unrelated}" in registrations, registrations
    assert recorded_branch_exists(repo, "user/temporarily-missing")


def test_clean_fanout_refuses_all_targets_if_one_is_dirty_or_unmerged(
    root: Path,
) -> None:
    repo, run_id, branch, integration, tasks, artifacts = _fanout_run(root)
    beta_branch, beta_worktree = tasks["beta"]
    (beta_worktree / "beta.txt").write_text("dirty\n")

    dirty = clean(repo, run_id)

    assert dirty.returncode == 1, dirty.stdout + dirty.stderr
    assert "is dirty" in dirty.stderr, dirty.stderr
    assert artifacts.exists() and integration.exists()
    assert all(
        path.exists() and recorded_branch_exists(repo, task_branch)
        for task_branch, path in tasks.values()
    )

    (beta_worktree / "beta.txt").write_text("beta\n")
    _commit_file(beta_worktree, "after-main.txt", "not merged\n")
    unmerged = clean(repo, run_id)

    assert unmerged.returncode == 1, unmerged.stdout + unmerged.stderr
    assert f"branch {beta_branch!r} is not merged into HEAD" in unmerged.stderr
    assert artifacts.exists() and integration.exists()
    assert recorded_branch_exists(repo, branch)
    assert all(
        path.exists() and recorded_branch_exists(repo, task_branch)
        for task_branch, path in tasks.values()
    )


def test_clean_fanout_preflights_a_locked_integration_worktree(root: Path) -> None:
    repo, run_id, branch, integration, tasks, artifacts = _fanout_run(root)
    subprocess.run(
        ["git", "worktree", "lock", str(integration)], cwd=repo, check=True
    )

    proc = clean(repo, run_id)

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert f"worktree {integration} is locked" in proc.stderr, proc.stderr
    assert artifacts.exists() and integration.exists()
    assert recorded_branch_exists(repo, branch)
    assert all(
        path.exists() and recorded_branch_exists(repo, task_branch)
        for task_branch, path in tasks.values()
    )


def test_clean_all_preflights_every_run_before_removing_any(root: Path) -> None:
    repo, run_id, branch, integration, tasks, artifacts = _fanout_run(root)
    safe_artifacts, safe_worktree, safe_branch = recorded_run(repo, "safe")
    subprocess.run(
        ["git", "merge", "-q", "--ff-only", safe_branch], cwd=repo, check=True
    )
    _, dirty_worktree = tasks["beta"]
    (dirty_worktree / "beta.txt").write_text("dirty\n")

    proc = clean(repo, "--all")

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "is dirty" in proc.stderr, proc.stderr
    assert artifacts.exists() and integration.exists()
    assert recorded_branch_exists(repo, branch)
    assert safe_artifacts.exists() and safe_worktree.exists()
    assert recorded_branch_exists(repo, safe_branch)
    assert all(
        path.exists() and recorded_branch_exists(repo, task_branch)
        for task_branch, path in tasks.values()
    )


def test_clean_fanout_rejects_a_task_target_borrowed_from_another_branch(
    root: Path,
) -> None:
    repo, run_id, branch, integration, tasks, artifacts = _fanout_run(root)
    foreign_branch = f"{branch}-foreign"
    foreign_worktree = root / "worktrees" / "foreign"
    _add_worktree(repo, foreign_worktree, foreign_branch)
    _commit_file(foreign_worktree, "foreign.txt", "foreign\n")
    subprocess.run(
        ["git", "merge", "-q", "--ff-only", foreign_branch],
        cwd=repo,
        check=True,
    )
    state_path = artifacts / "state.json"
    state = json.loads(state_path.read_text())
    state["fanout"]["tasks"]["alpha"].update(
        branch=foreign_branch,
        worktree=str(foreign_worktree),
    )
    state_path.write_text(json.dumps(state))

    proc = clean(repo, run_id)

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "does not match its recorded branch/worktree naming" in proc.stderr
    assert artifacts.exists() and integration.exists() and foreign_worktree.exists()
    assert recorded_branch_exists(repo, foreign_branch)
    assert all(
        path.exists() and recorded_branch_exists(repo, task_branch)
        for task_branch, path in tasks.values()
    )
