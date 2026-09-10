"""Keep long run names and task summaries readable without losing run details."""

from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from stargate.run import list_runs


def test_listing_separates_runs_and_wraps_task_summaries_with_visible_truncation(
    root: Path,
) -> None:
    task = "Reduzir a duplicação das receitas preservando todos os campos existentes."
    newer_id = "20260910-190331-c2-recipes-with-a-long-name"
    older_id = "20260910-182718-orders"
    branch = "stargate/c2-recipes-20260910-190331"
    for run_id, description in ((newer_id, task), (older_id, "very long task " * 100)):
        artifacts = root / ".stargate" / "runs" / run_id
        artifacts.mkdir(parents=True)
        (artifacts / "state.json").write_text(json.dumps({
            "run_id": run_id,
            "status": "approved",
            "stage": "review",
            "updated_at": "2026-09-10T19:18:44",
            "task": description,
            "branch": branch,
            "worktree": str(root),
        }))

    for width in (40, 80, 120):
        output = io.StringIO()
        with (
            patch(
                "stargate.run.shutil.get_terminal_size",
                return_value=os.terminal_size((width, 24)),
            ),
            redirect_stdout(output),
        ):
            assert list_runs(root) == 0
        listing = output.getvalue()
        assert "RUN ID" not in listing, listing
        assert f"  {newer_id}  [approved]" in listing, listing
        assert f"\n\n  {older_id}  [approved]" in listing, listing
        assert listing.index(newer_id) < listing.index(older_id), listing
        assert "updated   2026-09-10 19:18:44  |  stage review" in listing, listing
        assert f"branch    {branch}" in listing, listing
        assert f"worktree  {root}" in listing, listing
        blocks = listing.split("\n\n")[1:3]
        for index, block in enumerate(blocks):
            summary = [line for line in block.splitlines()
                       if line.startswith(("    task      ", "              "))]
            assert 1 <= len(summary) <= 3, listing
            assert all(len(line) <= width for line in summary), listing
            if index == 0:
                assert " ".join(summary)[14:].split() == task.split(), listing
            else:
                assert summary[-1].endswith(" ..."), listing
        assert "No runs are marked resumable." in listing, listing
