"""`watch` must show live fan-out progress without ever writing run bookkeeping."""

from __future__ import annotations

import json
from pathlib import Path

from tests.harness import make_repo, stargate


def test_watch_once_shows_live_task_progress_and_is_read_only(root: Path) -> None:
    repo = make_repo(root)
    run_id = "20260910-190331-demo"
    artifacts = repo / ".stargate" / "runs" / run_id
    (artifacts / "tasks" / "b").mkdir(parents=True)
    state_file = artifacts / "state.json"
    state_file.write_text(json.dumps({
        "run_id": run_id, "status": "running", "stage": "tasks",
        "task": "demo", "mode": "fanout", "tokens_used": 1234,
        "fanout": {
            "order": ["a", "b"],
            "tasks": {
                "a": {"status": "complete", "stage": "done", "tokens_used": 1000},
                # The parent record still holds what was written when b started.
                "b": {"status": "running", "stage": "developer",
                      "tokens_used": 0, "depends_on": ["a"]},
            },
        },
    }))
    task_state = artifacts / "tasks" / "b" / "state.json"
    task_state.write_text(json.dumps({"stage": "fixer", "tokens_used": 5678}))
    before = {p: p.read_text() for p in (state_file, task_state)}

    proc = stargate(repo, "watch", "--once", config_home=root / "config-home")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"* {run_id}  [running]" in proc.stdout, proc.stdout
    # The recorded 1,234 already counts a; b's 5,678 is only in its own state.
    assert "tokens 6,912" in proc.stdout, proc.stdout
    # Without reading the task's own state, b would show developer and 0 tokens.
    assert "fixer" in proc.stdout and "5,678 tok" in proc.stdout, proc.stdout
    assert "after a" in proc.stdout, proc.stdout
    assert {p: p.read_text() for p in before} == before


def test_watch_without_runs_does_not_create_bookkeeping(root: Path) -> None:
    repo = make_repo(root)

    proc = stargate(repo, "watch", "--once", config_home=root / "config-home")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "No recorded runs" in proc.stdout, proc.stdout
    assert not (repo / ".stargate").exists(), proc.stdout


def test_watch_rejects_an_interval_that_would_crash_the_loop(root: Path) -> None:
    repo = make_repo(root)

    proc = stargate(repo, "watch", "--interval", "0", config_home=root / "config-home")

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "positive number of seconds" in proc.stderr, proc.stderr
