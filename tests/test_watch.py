"""`watch` must show live fan-out progress without ever writing run bookkeeping."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from unittest.mock import patch

from stargate.core import StargateError
from stargate.watch import render, watch
from tests.harness import make_repo, stargate


def _write_run(repo: Path, run_id: str, state: dict) -> Path:
    artifacts = repo / ".stargate" / "runs" / run_id
    artifacts.mkdir(parents=True)
    (artifacts / "state.json").write_text(json.dumps({"run_id": run_id, **state}))
    return artifacts


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


def test_long_task_ids_keep_the_progress_columns_visible(root: Path) -> None:
    repo = make_repo(root)
    long_id = "t" * 48  # the longest ID the fan-out graph accepts
    _write_run(repo, "20260910-190331-demo", {
        "status": "running", "mode": "fanout",
        "fanout": {"order": [long_id], "tasks": {
            long_id: {"status": "complete", "stage": "done", "tokens_used": 4321},
        }},
    })

    proc = stargate(repo, "watch", "--once", config_home=root / "config-home",
                    env={"COLUMNS": "80"})

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "4,321 tok" in proc.stdout, proc.stdout


def test_one_damaged_run_does_not_hide_the_others(root: Path) -> None:
    repo = make_repo(root)
    _write_run(repo, "20260910-190331-broken", {
        "status": "running", "mode": "fanout", "tokens_used": "lots",
        "fanout": {"order": [["x"], "a"], "tasks": {
            "a": {"status": "running", "tokens_used": {}, "depends_on": "a"},
        }},
    })
    _write_run(repo, "20260909-120000-healthy", {"status": "approved", "tokens_used": 77})

    proc = stargate(repo, "watch", "--once", config_home=root / "config-home")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "20260910-190331-broken" in proc.stdout, proc.stdout
    assert "tokens 77" in proc.stdout, proc.stdout


def test_unreadable_runs_directory_is_an_error_not_an_empty_view(root: Path) -> None:
    (root / ".stargate" / "runs").mkdir(parents=True)

    # Simulated in-process: chmod does not stop root from reading the directory.
    with patch("pathlib.Path.iterdir", side_effect=PermissionError("denied")):
        try:
            render(root, 80)
        except StargateError as exc:
            assert "Could not read recorded runs" in str(exc), exc
        else:
            raise AssertionError("an unreadable runs directory rendered as empty")


def test_live_frame_fits_the_terminal_so_the_newest_run_stays_on_screen(root: Path) -> None:
    for day in range(1, 6):
        _write_run(root, f"2026091{day}-120000-run{day}", {
            "status": "running", "mode": "fanout",
            "fanout": {"order": list("abcdefgh"), "tasks": {
                t: {"status": "pending", "stage": "pending"} for t in "abcdefgh"
            }},
        })

    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    out = Tty()
    with patch("sys.stdout", out), \
            patch("shutil.get_terminal_size", return_value=os.terminal_size((80, 24))), \
            patch("time.sleep", side_effect=KeyboardInterrupt):
        assert watch(root, interval=1) == 0

    frame = out.getvalue().split("\x1b[H\x1b[J", 1)[1]
    assert frame.count("\n") < 24, frame
    assert "20260915-120000-run5" in frame, frame
    assert "more line(s); use --once" in frame, frame
