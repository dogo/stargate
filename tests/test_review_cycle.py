"""Regression tests for resuming into a recorded review/fix cycle."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from tests.harness import ROOT, agent, git_output, make_repo, run, runs

README = (ROOT / "README.md").read_text()


def _counting(marker: Path, script: str) -> list[str]:
    # `$0` holds the orchestrator's prompt; `$call` is how many times this
    # agent has been invoked across the whole run, resumes included.
    return agent(
        f'echo x >> {marker}; call=$(wc -l < {marker} | tr -d " "); {script}'
    )


def _calls(marker: Path) -> int:
    return len(marker.read_text().splitlines()) if marker.exists() else 0


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


def _run_state(repo: Path) -> tuple[Path, dict]:
    state_path = next((repo / ".stargate" / "runs").glob("*/state.json"))
    return state_path, json.loads(state_path.read_text())


def test_a_failed_fixer_resumes_into_the_fixer_not_a_new_review(
    root: Path,
) -> None:
    repo = make_repo(root)
    reviews = root / "reviews.txt"
    fixes = root / "fixes.txt"
    config = root / "cycle.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "agents": {
                    "noop": {"command": agent("echo done")},
                    "dev": {"command": agent("echo change >> impl.txt")},
                    "rev": {
                        "command": _counting(
                            reviews, 'echo "VERDICT: CHANGES_REQUESTED"'
                        )
                    },
                    # The vendor limit that started all this: the first fixer
                    # dies before touching a file, so the tree the reviewer
                    # judged is still exactly what is on disk.
                    "fix": {
                        "command": _counting(
                            fixes,
                            'if [ "$call" = 1 ]; then echo limit >&2; exit 7; '
                            "fi; echo fixed >> impl.txt",
                        )
                    },
                },
                "workflow": {
                    "architect": "noop",
                    "developer": "dev",
                    "reviewer": "rev",
                    "fixer": "fix",
                },
                "settings": {
                    "max_review_loops": 1,
                    "test_command": "true",
                    "agent_timeout_seconds": 60,
                },
            }
        )
    )

    first = run(repo, config, "resume into the fixer")
    assert first.returncode != 0, first.stdout + first.stderr
    state_path, state = _run_state(repo)
    assert state["review"] == {
        "attempt": 1,
        "verdict": "CHANGES_REQUESTED",
        "fingerprint": state["review"]["fingerprint"],
        "fixed": False,
    }, state["review"]
    assert _calls(reviews) == 1 and _calls(fixes) == 1

    resumed = _resume(repo, config, state["run_id"])

    assert "=== REVIEW 1 (skipped, reusing" in resumed.stdout, resumed.stdout
    assert "=== FIXER 1 ===" in resumed.stdout, resumed.stdout
    # Two reviews total, not three: the interrupted pass reused the verdict it
    # had already paid for, and only the review its fixer earned is new.
    assert _calls(reviews) == 2, resumed.stdout
    assert _calls(fixes) == 2, resumed.stdout
    assert "=== REVIEW 2 ===" in resumed.stdout, resumed.stdout


def test_an_edited_worktree_invalidates_the_recorded_review(root: Path) -> None:
    repo = make_repo(root)
    reviews = root / "reviews.txt"
    config = root / "invalidate.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "agents": {
                    "noop": {"command": agent("echo done")},
                    "dev": {"command": agent("echo change >> impl.txt")},
                    "rev": {
                        "command": _counting(
                            reviews, 'echo "VERDICT: CHANGES_REQUESTED"'
                        )
                    },
                    "fix": {"command": agent("echo limit >&2; exit 7")},
                },
                "workflow": {
                    "architect": "noop",
                    "developer": "dev",
                    "reviewer": "rev",
                    "fixer": "fix",
                },
                "settings": {
                    "max_review_loops": 1,
                    "test_command": "true",
                    "agent_timeout_seconds": 60,
                },
            }
        )
    )

    first = run(repo, config, "invalidate the record")
    assert first.returncode != 0, first.stdout + first.stderr
    state_path, state = _run_state(repo)
    # Hand-edit the tree the reviewer judged. Its verdict no longer describes
    # what is on disk, so it must not be reused.
    (Path(state["worktree"]) / "impl.txt").write_text("hand edited\n")

    resumed = _resume(repo, config, state["run_id"])

    assert "REVIEW 1 (skipped" not in resumed.stdout, resumed.stdout
    assert "=== REVIEW 1 ===" in resumed.stdout, resumed.stdout
    assert _calls(reviews) == 2, resumed.stdout


def test_a_failed_commit_resumes_straight_into_the_commit(root: Path) -> None:
    repo = make_repo(root)
    reviews = root / "reviews.txt"
    config = root / "commit.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "agents": {
                    "noop": {"command": agent("echo done")},
                    "dev": {"command": agent("echo change >> impl.txt")},
                    "rev": {
                        "command": _counting(reviews, 'echo "VERDICT: APPROVED"')
                    },
                },
                "workflow": {
                    "architect": "noop",
                    "developer": "dev",
                    "reviewer": "rev",
                    "fixer": "dev",
                },
                "settings": {
                    "max_review_loops": 0,
                    "test_command": "true",
                    "agent_timeout_seconds": 60,
                },
            }
        )
    )
    # Stands in for the signing prompt that timed out: the verdict is reached,
    # the tree is final, and only `git commit` refuses. Hooks live in the
    # common Git directory, so a linked worktree runs this one too.
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)

    blocked = run(repo, config, "retry only the commit")
    assert blocked.returncode == 5, blocked.stdout + blocked.stderr
    state_path, state = _run_state(repo)
    branch = state["branch"]
    assert state["commit_error"], state
    assert state["review"]["verdict"] == "APPROVED", state["review"]
    assert git_output(repo, "rev-list", "--count", f"main..{branch}") == "0"
    assert f"stargate resume {state['run_id']}" in state["commit_error"]

    # A run blocked only on its commit is the one that needs resuming, whatever
    # terminal status it recorded.
    listing = runs(repo)
    assert listing.returncode == 0, listing.stderr
    assert f"* {state['run_id']}" in listing.stdout, listing.stdout

    hook.unlink()
    resumed = _resume(repo, config, state["run_id"])

    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert "(skipped, already APPROVED)" in resumed.stdout, resumed.stdout
    # The whole point: the retry costs no second opinion.
    assert _calls(reviews) == 1, resumed.stdout
    assert git_output(repo, "rev-list", "--count", f"main..{branch}") == "1"
    _, settled = _run_state(repo)
    assert not settled["commit_error"], settled
    # A finished run stops carrying the record, so resuming it again is a
    # deliberate request for a fresh verdict.
    assert not settled.get("review"), settled


def test_a_resumed_loop_continues_the_review_budget(root: Path) -> None:
    repo = make_repo(root)
    reviews = root / "reviews.txt"
    fixes = root / "fixes.txt"
    config = root / "budget.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "agents": {
                    "noop": {"command": agent("echo done")},
                    "dev": {"command": agent("echo change >> impl.txt")},
                    # Dies on the second review, after the first fixer pass has
                    # already been paid for and recorded.
                    "rev": {
                        "command": _counting(
                            reviews,
                            'if [ "$call" = 2 ]; then echo limit >&2; exit 7; '
                            'fi; echo "VERDICT: CHANGES_REQUESTED"',
                        )
                    },
                    "fix": {
                        "command": _counting(fixes, "echo fixed >> impl.txt")
                    },
                },
                "workflow": {
                    "architect": "noop",
                    "developer": "dev",
                    "reviewer": "rev",
                    "fixer": "fix",
                },
                "settings": {
                    "max_review_loops": 1,
                    "test_command": "true",
                    "agent_timeout_seconds": 60,
                },
            }
        )
    )

    first = run(repo, config, "continue the budget")
    assert first.returncode != 0, first.stdout + first.stderr
    _, state = _run_state(repo)
    assert state["review"]["fixed"] is True, state["review"]
    assert _calls(fixes) == 1

    resumed = _resume(repo, config, state["run_id"])

    # The loop picks up at the review the fixer earned, not back at the first
    # one, so max_review_loops stays a budget for the run.
    assert "=== REVIEW 2 ===" in resumed.stdout, resumed.stdout
    assert "=== REVIEW 1" not in resumed.stdout, resumed.stdout
    assert resumed.returncode == 2, resumed.stdout + resumed.stderr
    assert _calls(fixes) == 1, resumed.stdout
    assert _calls(reviews) == 3, resumed.stdout


def test_readme_describes_resuming_into_the_review_cycle(root: Path) -> None:
    compact = " ".join(README.split())

    assert "resumes straight into that fixer" in compact
    assert "resumes straight into the commit" in compact
    assert "REVIEW <n> (skipped, already APPROVED)" in compact
    assert "marked resumable by `stargate list`" in compact
    assert (
        "`max_review_loops` is a budget for the run, not a fresh allowance "
        "per resume" in compact
    )
    assert "A verdict never covers a tree its reviewer did not see." in compact
    # The old promise is gone: it is the behaviour this change replaced.
    assert "review loop always restarts from its first attempt" not in compact
