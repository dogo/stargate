"""Shared helpers for Stargate's subprocess and Git integration tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sh(cmd: str, cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, shell=True, check=True, capture_output=True)


def git_output(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, check=True
    ).stdout.strip()


def make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    sh(
        "git init -q -b main && git config user.email t@t && "
        "git config user.name t && git config commit.gpgsign false",
        repo,
    )
    (repo / "app.py").write_text("x = 1\n")
    sh("git add -A && git commit -qm init", repo)
    return repo


def agent(script: str) -> list[str]:
    # `sh -c script <prompt>` puts the orchestrator's prompt in $0.
    return ["/bin/sh", "-c", script]


def write_config(
    path: Path,
    reviewer: str,
    *,
    test_command: str,
    loops: int = 0,
    agent_timeout: int = 60,
    prompts_dir: str = "",
    test_command_detection: str | None = None,
    commit: bool | str | None = None,
    reviewer_args: tuple[str, ...] = (),
) -> None:
    import yaml

    reviewer_command = agent(reviewer)
    if reviewer_args:
        # Preserve $0 for the shell while exposing configured argv from $1 on.
        reviewer_command = [*reviewer_command, "_", *reviewer_args]
    cfg = {
        "agents": {
            "noop": {"command": agent('printf "%s" "$0" > /dev/null; echo done')},
            "dev": {"command": agent("echo change >> impl.txt; echo done")},
            "reviewer": {"command": reviewer_command},
        },
        "workflow": {
            "architect": "noop",
            "developer": "dev",
            "reviewer": "reviewer",
            "fixer": "dev",
        },
        "settings": {
            "max_review_loops": loops,
            "test_command": test_command,
            "agent_timeout_seconds": agent_timeout,
            "test_timeout_seconds": 30,
            "prompts_dir": prompts_dir,
        },
    }
    if test_command_detection is not None:
        cfg["settings"]["test_command_detection"] = test_command_detection
    if commit is not None:
        cfg["settings"]["commit"] = commit
    path.write_text(yaml.safe_dump(cfg))


def write_fanout_config(
    path: Path,
    graph: Path,
    developer: str,
    *,
    reviewer: str = 'echo "VERDICT: APPROVED"',
    architect_marker: Path | None = None,
    max_parallel: int = 2,
) -> None:
    import yaml

    marker = f"echo called >> {architect_marker}; " if architect_marker else ""
    cfg = {
        "agents": {
            "arch": {"command": agent(f"{marker}cat {graph}")},
            "dev": {"command": agent(developer)},
            "rev": {"command": agent(reviewer)},
        },
        "workflow": {
            "architect": "arch",
            "developer": "dev",
            "reviewer": "rev",
            "fixer": "dev",
        },
        "settings": {
            "max_review_loops": 0,
            "test_command": "true",
            "max_parallel_tasks": max_parallel,
        },
    }
    path.write_text(yaml.safe_dump(cfg))


def run(
    repo: Path, config: Path, task: str = "demo task", *run_args: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "stargate", "--config", str(config), "run", *run_args, task],
        cwd=repo,
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )


def doctor(repo: Path, config: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "stargate", "--config", str(config), "doctor", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )


def runs(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "stargate", "list"],
        cwd=repo,
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )


def clean(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "stargate", "clean", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )


def stargate(
    repo: Path, *args: str, config_home: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "stargate", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "XDG_CONFIG_HOME": str(config_home),
        },
    )


def recorded_run(repo: Path, run_id: str) -> tuple[Path, Path, str]:
    branch = f"stargate/{run_id}"
    worktree = repo.parent / "worktrees" / run_id
    worktree.parent.mkdir(exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", branch, str(worktree)],
        cwd=repo,
        check=True,
    )
    (worktree / f"{run_id}.txt").write_text("change\n")
    sh("git add -A && git commit -qm change", worktree)
    artifacts = repo / ".stargate" / "runs" / run_id
    artifacts.mkdir(parents=True)
    (artifacts / "state.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "repo": str(repo),
                "branch": branch,
                "worktree": str(worktree),
            }
        )
    )
    return artifacts, worktree, branch


def recorded_branch_exists(repo: Path, branch: str) -> bool:
    return (
        subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=repo,
        ).returncode
        == 0
    )
