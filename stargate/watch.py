"""Read-only live view of the runs recorded in a repository.

Only reads each run's state.json; it never writes bookkeeping and needs no
config, so it is safe beside a running orchestrator.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from .core import StargateError
from .run import read_run

MAX_RUNS = 5
TASK_ID_WIDTH = 16


def _load_state(path: Path) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _int(value: Any) -> int:
    # A damaged field must not stop the view of every other run.
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(0, width - 3)] + "..."


def _task_lines(path: Path, fanout: dict[str, Any], width: int) -> tuple[list[str], int]:
    """Return the task rows and the tokens running tasks spent beyond their record."""
    records = fanout.get("tasks")
    order = fanout.get("order")
    if not isinstance(records, dict) or not isinstance(order, list):
        return [], 0
    lines = []
    unrecorded = 0
    for task_id in order:
        record = records.get(task_id) if isinstance(task_id, str) else None
        if not isinstance(record, dict):
            continue
        status = str(record.get("status", "?"))
        stage = record.get("stage", "-")
        tokens = _int(record.get("tokens_used"))
        if status == "running":
            # The parent record is only updated when a task ends; the live
            # stage and tokens are in the task's own state.json.
            live = _load_state(path / "tasks" / task_id / "state.json")
            stage = live.get("stage") or stage
            live_tokens = _int(live.get("tokens_used"))
            # The run total absorbs this delta only when the task ends.
            unrecorded += max(0, live_tokens - tokens)
            tokens = max(tokens, live_tokens)
        deps = record.get("depends_on")
        deps = ",".join(map(str, deps)) if isinstance(deps, list) else ""
        lines.append(_clip(
            f"      {_clip(task_id, TASK_ID_WIDTH):<{TASK_ID_WIDTH}} {status:<10} "
            f"{str(stage):<10} {tokens:>9,} tok"
            + (f"  after {deps}" if deps else ""),
            width,
        ))
    return lines, unrecorded


def render(repo: Path, width: int, limit: int = MAX_RUNS) -> str:
    root = repo / ".stargate" / "runs"
    lines = [f"stargate watch  {repo}", ""]
    if not root.is_dir():
        lines.append(f"No recorded runs in {root}")
        return "\n".join(lines)
    try:
        paths = sorted(
            (p for p in root.iterdir() if p.is_dir()),
            key=lambda p: p.name,
            reverse=True,
        )
    except OSError as exc:
        raise StargateError(f"Could not read recorded runs in {root}: {exc}") from exc
    if not paths:
        lines.append(f"No recorded runs in {root}")
        return "\n".join(lines)

    for path in paths[:limit]:
        row = read_run(path)
        state = _load_state(path / "state.json")
        marker = "*" if row["resumable"] else " "
        lines.append(_clip(f"{marker} {row['run_id']}  [{row['status']}]", width))
        fanout = state.get("fanout")
        tasks, unrecorded = (
            _task_lines(path, fanout, width)
            if state.get("mode") == "fanout" and isinstance(fanout, dict)
            else ([], 0)
        )
        tokens = _int(state.get("tokens_used")) + unrecorded
        lines.append(_clip(
            f"    stage {row['stage']}  |  tokens {tokens:,}  |  updated "
            f"{row['updated'].replace('T', ' ', 1)}",
            width,
        ))
        lines.append(_clip(f"    task  {row['task']}", width))
        if row["error"]:
            lines.append(_clip(f"    {row['error']}", width))
        elif state.get("error"):
            lines.append(_clip(f"    error {' '.join(str(state['error']).split())}", width))
        lines.extend(tasks)
        lines.append("")
    hidden = len(paths) - limit
    if hidden > 0:
        lines.append(f"... {hidden} older run(s); see 'stargate list'")
    return "\n".join(lines).rstrip()


def _fit_height(frame: str, height: int) -> str:
    # Rows beyond the screen would scroll the newest run off the top each refresh.
    lines = frame.split("\n")
    budget = max(2, height - 3)  # blank line, exit hint, trailing newline
    if len(lines) <= budget:
        return frame
    kept = lines[: budget - 1]
    return "\n".join(kept + [f"... {len(lines) - len(kept)} more line(s); use --once"])


def watch(repo: Path, interval: float = 2.0, once: bool = False) -> int:
    live = not once and sys.stdout.isatty()
    try:
        while True:
            size = shutil.get_terminal_size()
            frame = render(repo, max(20, size.columns))
            if not live:
                print(frame)
                return 0
            # Home the cursor and clear below so frames do not flicker.
            sys.stdout.write(
                "\x1b[H\x1b[J" + _fit_height(frame, size.lines) + "\n\nCtrl-C to exit.\n"
            )
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0
