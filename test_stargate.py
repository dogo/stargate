#!/usr/bin/env python3
"""Smoke test: fake agents, real git worktrees, real orchestrator.

Run with: .venv/bin/python test_stargate.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def sh(cmd: str, cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, shell=True, check=True, capture_output=True)


def make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    sh("git init -q -b main && git config user.email t@t && git config user.name t", repo)
    (repo / "app.py").write_text("x = 1\n")
    sh("git add -A && git commit -qm init", repo)
    return repo


def agent(script: str) -> list[str]:
    # `sh -c script <prompt>` puts the orchestrator's prompt in $0.
    return ["/bin/sh", "-c", script]


def write_config(path: Path, reviewer: str, *, test_command: str, loops: int = 0,
                 agent_timeout: int = 60, prompts_dir: str = "") -> None:
    import yaml
    cfg = {
        "agents": {
            "noop": {"command": agent('printf "%s" "$0" > /dev/null; echo done')},
            "reviewer": {"command": agent(reviewer)},
        },
        "workflow": {"architect": "noop", "developer": "noop",
                     "reviewer": "reviewer", "fixer": "noop"},
        "settings": {"max_review_loops": loops, "test_command": test_command,
                     "agent_timeout_seconds": agent_timeout, "test_timeout_seconds": 30,
                     "prompts_dir": prompts_dir},
    }
    path.write_text(yaml.safe_dump(cfg))


def run(repo: Path, config: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "stargate", "--config", str(config), "run", "demo task"],
        cwd=repo, text=True, capture_output=True,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )


def test_prose_mentioning_approved_is_not_an_approval(root: Path) -> None:
    repo = make_repo(root)
    cfg = root / "a.yaml"
    write_config(cfg, 'echo "I cannot give VERDICT: APPROVED yet."; echo "VERDICT: CHANGES_REQUESTED"',
                 test_command="true")
    proc = run(repo, cfg)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "Verdict:   CHANGES_REQUESTED" in proc.stdout, proc.stdout


def test_reviewer_receives_test_output(root: Path) -> None:
    repo = make_repo(root)
    cfg = root / "b.yaml"
    dump = root / "reviewer-prompt.txt"
    write_config(cfg, f'printf "%s" "$0" > {dump}; echo "VERDICT: APPROVED"',
                 test_command="echo MARKER_TEST_RAN; exit 3")
    proc = run(repo, cfg)
    prompt = dump.read_text()
    assert "MARKER_TEST_RAN" in prompt, "reviewer never saw the test output"
    assert "FAILED (exit 3)" in prompt, prompt
    # Approved but tests red -> non-zero exit.
    assert proc.returncode == 3, proc.stdout + proc.stderr


def test_missing_verdict_is_an_error(root: Path) -> None:
    repo = make_repo(root)
    cfg = root / "c.yaml"
    write_config(cfg, 'echo "looks fine to me"', test_command="true")
    proc = run(repo, cfg)
    assert proc.returncode == 1, proc.stdout
    assert "recognized verdict" in proc.stderr, proc.stderr


def test_custom_prompts_dir_overrides_one_file(root: Path) -> None:
    repo = make_repo(root)
    custom = root / "myprompts"
    custom.mkdir()
    # Only reviewer.md is overridden; the other three must still resolve.
    (custom / "reviewer.md").write_text(
        "CUSTOM_REVIEWER {task} {base_ref} {plan} {tests}\nVERDICT line goes last.\n"
    )
    dump = root / "reviewer-prompt.txt"
    cfg = root / "e.yaml"
    write_config(cfg, f'printf "%s" "$0" > {dump}; echo "VERDICT: APPROVED"',
                 test_command="true", prompts_dir=str(custom))
    proc = run(repo, cfg)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    prompt = dump.read_text()
    assert prompt.startswith("CUSTOM_REVIEWER"), prompt[:200]
    assert "senior code reviewer" not in prompt, "packaged reviewer.md leaked in"


def test_output_placeholder_forwards_file_not_stdout(root: Path) -> None:
    """A codex-style agent: noisy stdout, real answer in --output-last-message."""
    repo = make_repo(root)
    dump = root / "reviewer-prompt.txt"
    cfg = root / "f.yaml"
    import yaml
    noisy = ["/bin/sh", "-c",
             'echo "TRACE: ran 40 commands, 118089 tokens"; echo "REAL_PLAN" > "$1"',
             "_", "{output}"]
    cfg.write_text(yaml.safe_dump({
        "agents": {
            "arch": {"command": noisy},
            "noop": {"command": agent("echo done")},
            "rev": {"command": agent(f'printf "%s" "$0" > {dump}; echo "VERDICT: APPROVED"')},
        },
        "workflow": {"architect": "arch", "developer": "noop",
                     "reviewer": "rev", "fixer": "noop"},
        "settings": {"max_review_loops": 0, "test_command": "true"},
    }))
    proc = run(repo, cfg)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    prompt = dump.read_text()
    assert "REAL_PLAN" in prompt, "final message never reached the next role"
    assert "118089 tokens" not in prompt, "stdout trace leaked into the next prompt"


def test_output_placeholder_empty_file_is_an_error(root: Path) -> None:
    repo = make_repo(root)
    cfg = root / "g.yaml"
    import yaml
    cfg.write_text(yaml.safe_dump({
        "agents": {"arch": {"command": ["/bin/sh", "-c", "echo noise", "_", "{output}"]},
                   "noop": {"command": agent("echo done")}},
        "workflow": {"architect": "arch", "developer": "noop",
                     "reviewer": "noop", "fixer": "noop"},
        "settings": {"max_review_loops": 0, "test_command": "true"},
    }))
    proc = run(repo, cfg)
    assert proc.returncode == 1, proc.stdout
    assert "wrote nothing" in proc.stderr, proc.stderr


def test_hung_agent_times_out(root: Path) -> None:
    repo = make_repo(root)
    cfg = root / "d.yaml"
    write_config(cfg, "sleep 30", test_command="true", agent_timeout=2)
    proc = run(repo, cfg)
    assert proc.returncode == 1, proc.stdout
    assert "timed out" in proc.stderr, proc.stderr


if __name__ == "__main__":
    for fn in (test_prose_mentioning_approved_is_not_an_approval,
               test_reviewer_receives_test_output,
               test_missing_verdict_is_an_error,
               test_custom_prompts_dir_overrides_one_file,
               test_output_placeholder_forwards_file_not_stdout,
               test_output_placeholder_empty_file_is_an_error,
               test_hung_agent_times_out):
        with tempfile.TemporaryDirectory() as tmp:
            fn(Path(tmp))
        print(f"ok  {fn.__name__}")
    print("\nall good")
