"""Recorded run text is shown to a terminal, so it must not be able to drive it.

A task can come from a fetched URL and stages from any state.json; an escape
sequence in either could clear or spoof what `list` and `watch` display.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.harness import make_repo, stargate

CLEAR = "\x1b[2J\x1b[H"
CSI = "\x9b"  # the single-byte C1 form of ESC [


def _hostile_run(repo: Path) -> None:
    artifacts = repo / ".stargate" / "runs" / "20260910-190331-demo"
    artifacts.mkdir(parents=True)
    (artifacts / "state.json").write_text(json.dumps({
        "run_id": "20260910-190331-demo", "status": "running", "mode": "fanout",
        "stage": f"tasks{CSI}2J", "task": f"{CLEAR}Fake approved run",
        "branch": f"stargate/demo{CLEAR}", "error": f"boom{CLEAR}",
        "fanout": {"order": ["a"], "tasks": {
            "a": {"status": f"complete{CLEAR}", "stage": f"done{CLEAR}",
                  "depends_on": [f"x{CLEAR}"]},
        }},
    }))


def test_list_drops_control_characters_from_recorded_text(root: Path) -> None:
    repo = make_repo(root)
    _hostile_run(repo)

    proc = stargate(repo, "list", config_home=root / "config-home")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "\x1b" not in proc.stdout and CSI not in proc.stdout, repr(proc.stdout)
    # The text itself stays visible, so a tampered record is noticed, not hidden.
    assert "Fake approved run" in proc.stdout, proc.stdout


def test_watch_drops_control_characters_from_recorded_text(root: Path) -> None:
    repo = make_repo(root)
    _hostile_run(repo)

    proc = stargate(repo, "watch", "--once", config_home=root / "config-home")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "\x1b" not in proc.stdout and CSI not in proc.stdout, repr(proc.stdout)
    assert "Fake approved run" in proc.stdout and "after x" in proc.stdout, proc.stdout
