"""Regression coverage for the shared fan-out terminal-result path."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from stargate.core import RunContext
from stargate.stages import write_summary
from tests.harness import (
    ROOT,
    git_output,
    make_repo,
    run,
    write_config,
    write_fanout_config,
)


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


def _single_task_graph(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "name": "terminal result",
                "tasks": [
                    {
                        "id": "unit",
                        "task": "Create the unit change.",
                        "depends_on": [],
                        "acceptance": ["unit.txt exists"],
                    }
                ],
            }
        )
    )


def test_fanout_terminal_commit_is_recovered_and_tracks_verdict_changes(
    root: Path,
) -> None:
    repo = make_repo(root)
    graph = root / "tasks.json"
    _single_task_graph(graph)
    config = root / "fanout.yaml"
    write_fanout_config(config, graph, "echo unit > unit.txt")

    first = run(repo, config, "terminal result", "--fan-out")
    assert first.returncode == 0, first.stdout + first.stderr
    state_path = next((repo / ".stargate" / "runs").glob("*/state.json"))
    state = json.loads(state_path.read_text())
    branch = state["branch"]
    worktree = Path(state["worktree"])
    terminal_commit = state["fanout"]["terminal_commit"]
    initial_count = int(git_output(repo, "rev-list", "--count", f"main..{branch}"))
    assert terminal_commit == git_output(worktree, "rev-parse", "HEAD")

    # Reproduce the durable crash window: Git completed the terminal commit,
    # while state.json still contains the preceding integration checkpoint.
    state["commit"] = git_output(worktree, "rev-parse", f"{terminal_commit}^")
    state["fanout"].pop("terminal_commit")
    state["fanout"].pop("terminal_verdict")
    state["status"] = "failed"
    state_path.write_text(json.dumps(state, indent=2) + "\n")

    same = _resume(repo, config, state["run_id"])
    assert same.returncode == 0, same.stdout + same.stderr
    recovered = json.loads(state_path.read_text())
    assert recovered["fanout"]["terminal_commit"] == terminal_commit
    assert recovered["fanout"]["terminal_verdict"] == "APPROVED"
    assert int(git_output(repo, "rev-list", "--count", f"main..{branch}")) == initial_count

    write_fanout_config(
        config,
        graph,
        "echo unit > unit.txt",
        reviewer='echo "VERDICT: CHANGES_REQUESTED"',
    )
    changed = _resume(repo, config, state["run_id"])
    assert changed.returncode == 2, changed.stdout + changed.stderr
    changed_state = json.loads(state_path.read_text())
    changed_count = int(git_output(repo, "rev-list", "--count", f"main..{branch}"))
    assert changed_count == initial_count + 1
    assert changed_state["fanout"]["terminal_verdict"] == "CHANGES_REQUESTED"
    assert changed_state["fanout"]["terminal_commit"] == changed_state["commit"]

    for _ in range(2):
        repeated = _resume(repo, config, state["run_id"])
        assert repeated.returncode == 2, repeated.stdout + repeated.stderr
        assert int(
            git_output(repo, "rev-list", "--count", f"main..{branch}")
        ) == changed_count


def test_fanout_summary_reports_task_tokens_errors_and_missing_records(
    root: Path,
) -> None:
    repo = make_repo(root)
    artifacts = repo / ".stargate" / "runs" / "summary-case"
    artifacts.mkdir(parents=True)
    base_commit = git_output(repo, "rev-parse", "HEAD")
    ctx = RunContext(
        repo=repo,
        config={"settings": {"max_task_tokens": 5000}},
        run_id="summary-case",
        slug="summary-case",
        branch="main",
        base_ref="main",
        base_commit=base_commit,
        worktree=repo,
        artifacts=artifacts,
        task="summarize failures",
        tokens_used=1234,
        mode="fanout",
        fanout={
            "order": ["failed-task", "blocked-task", "missing-task"],
            "tasks": {
                "failed-task": {
                    "status": "failed",
                    "test_exit": 7,
                    "tokens_used": 1234,
                    "commit": "abcdef1234567890",
                    "error": "developer crashed | exit 7\ntrace tail",
                },
                "blocked-task": {
                    "status": "blocked",
                    "test_exit": None,
                    "tokens_used": 0,
                    "commit": "",
                    "error": "Dependency failed-task did not complete.",
                },
            },
        },
    )

    write_summary(ctx, ctx.task, "FAILED", None, True)

    summary = (artifacts / "summary.md").read_text()
    assert "| task | status | tests | tokens | commit | error |" in summary
    assert (
        "| failed-task | failed | 7 | 1,234 | abcdef123456 | "
        "developer crashed \\| exit 7<br>trace tail |"
    ) in summary
    assert (
        "| blocked-task | blocked | not run | 0 | - | "
        "Dependency failed-task did not complete. |"
    ) in summary
    assert (
        "| missing-task | missing | not run | - | - | "
        "Task record is missing from state.json. |"
    ) in summary


def test_failed_fanout_writes_task_failure_reasons_to_summary(root: Path) -> None:
    repo = make_repo(root)
    graph = root / "failed-tasks.json"
    graph.write_text(
        json.dumps(
            {
                "name": "failure summary",
                "tasks": [
                    {"id": "alpha", "task": "complete alpha", "depends_on": []},
                    {"id": "beta", "task": "fail beta", "depends_on": []},
                    {
                        "id": "blocked",
                        "task": "wait for beta",
                        "depends_on": ["beta"],
                    },
                ],
            }
        )
    )
    config = root / "failed.yaml"
    developer = (
        'case "$0" in '
        '*"ASSIGNED TASK ID: alpha"*) echo "tokens 1234"; echo alpha > alpha.txt ;; '
        '*"ASSIGNED TASK ID: beta"*) echo boom >&2; exit 7 ;; '
        "*) exit 9 ;; esac"
    )
    write_fanout_config(config, graph, developer)
    data = yaml.safe_load(config.read_text())
    data["agents"]["dev"]["usage_pattern"] = r"tokens (\d+)"
    config.write_text(yaml.safe_dump(data))

    result = run(repo, config, "summarize failed tasks", "--fan-out")
    assert result.returncode == 1, result.stdout + result.stderr
    state_path = next((repo / ".stargate" / "runs").glob("*/state.json"))
    summary_path = state_path.parent / "summary.md"
    assert summary_path.exists(), result.stdout + result.stderr
    summary = summary_path.read_text()

    assert "| alpha | complete | 0 | 1,234 |" in summary
    assert "| beta | failed | not run | 0 | - | StargateError:" in summary
    assert "Command failed with exit code 7" in summary
    assert (
        "| blocked | blocked | not run | 0 | - | "
        "A dependency did not complete. |"
    ) in summary


def test_resume_rejects_missing_unreadable_non_object_and_modeless_state(
    root: Path,
) -> None:
    repo = make_repo(root)
    config = root / "resume.yaml"
    write_config(config, 'echo "VERDICT: APPROVED"', test_command="true")
    runs = repo / ".stargate" / "runs"
    cases = {
        "missing": (None, "No run state at"),
        "unreadable": ("not json {{{\n", "unreadable state.json"),
        "non-object": ("[]\n", "state.json is not a JSON object"),
        "mode-less": ("{}\n", "state.json has no valid mode"),
    }

    for run_id, (contents, expected) in cases.items():
        if contents is not None:
            artifacts = runs / run_id
            artifacts.mkdir(parents=True, exist_ok=True)
            (artifacts / "state.json").write_text(contents)
        resumed = _resume(repo, config, run_id)
        output = resumed.stdout + resumed.stderr
        assert resumed.returncode == 1, output
        assert expected in resumed.stderr, output
        assert "Traceback" not in output, output


def test_fanout_result_identifies_integration_only_fixer_edits(root: Path) -> None:
    repo = make_repo(root)
    graph = root / "tasks.json"
    _single_task_graph(graph)
    review_started = root / "review-started"
    config = root / "fixer.yaml"
    developer = (
        'case "$0" in *"ASSIGNED TASK ID:"*) echo task > unit.txt ;; '
        "*) echo fixed > integration-fix.txt ;; esac"
    )
    reviewer = (
        f"if test -e {review_started}; then "
        'echo "VERDICT: APPROVED"; else '
        f"touch {review_started}; "
        'echo "VERDICT: CHANGES_REQUESTED"; fi'
    )
    write_fanout_config(
        config,
        graph,
        developer,
        reviewer=reviewer,
        max_parallel=1,
    )
    data = yaml.safe_load(config.read_text())
    data["settings"]["max_review_loops"] = 1
    config.write_text(yaml.safe_dump(data))

    result = run(repo, config, "review fixer scope", "--fan-out")
    assert result.returncode == 0, result.stdout + result.stderr
    state_path = next((repo / ".stargate" / "runs").glob("*/state.json"))
    state = json.loads(state_path.read_text())
    integration = Path(state["worktree"])
    task_worktree = Path(state["fanout"]["tasks"]["unit"]["worktree"])
    summary = (state_path.parent / "summary.md").read_text()

    assert (integration / "integration-fix.txt").read_text() == "fixed\n"
    assert not (task_worktree / "integration-fix.txt").exists()
    assert "Review/fix: integration worktree only" in result.stdout
    assert "fixer edits were not copied back to task branches" in result.stdout
    assert "Review/fixer scope: integration worktree only" in summary
    assert "fixer edits are not copied back to task branches" in summary
