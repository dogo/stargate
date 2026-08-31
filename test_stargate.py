#!/usr/bin/env python3
"""Smoke test: fake agents, real git worktrees, real orchestrator.

Run with: .venv/bin/python test_stargate.py
"""
from __future__ import annotations

import json
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


def doctor(repo: Path, config: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "stargate", "--config", str(config),
         "doctor", *args],
        cwd=repo, text=True, capture_output=True,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )


def test_doctor_probe_is_opt_in_and_deduplicated(root: Path) -> None:
    repo = make_repo(root)
    cfg = root / "doctor.yaml"
    calls = root / "calls.txt"
    import yaml
    first = agent(f'echo "first:$0" >> {calls}; echo OK')
    second = ["/bin/sh", "-c",
              f'test -d .git && echo "$1" | grep -q "output-.*\\.txt" && '
              f'echo second >> {calls} && echo OK > "$1"', "_", "{output}"]
    cfg.write_text(yaml.safe_dump({
        "agents": {
            "architect": {"command": first, "probe": "cheap"},
            "reviewer": {"command": first, "probe": "cheap"},
            "developer": {"command": second, "probe": "cheap"},
            "fixer": {"command": second, "probe": "cheap"},
        },
        "workflow": {role: role for role in
                     ("architect", "developer", "reviewer", "fixer")},
        "settings": {},
    }))
    proc = doctor(repo, cfg)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not calls.exists(), "plain doctor made a billable probe"

    proc = doctor(repo, cfg, "--probe")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    recorded = calls.read_text().splitlines()
    assert len(recorded) == 2, proc.stdout
    assert "first:cheap" in recorded, recorded
    assert "OK   architect, reviewer [" in proc.stdout, proc.stdout
    assert "OK   developer, fixer [" in proc.stdout, proc.stdout


def test_doctor_probe_reports_cli_failure(root: Path) -> None:
    repo = make_repo(root)
    cfg = root / "doctor-fail.yaml"
    import yaml
    cfg.write_text(yaml.safe_dump({
        "agents": {"bad": {
            "command": agent('echo "Credit balance is too low" >&2; exit 7'),
            "probe": "cheap",
        }},
        "workflow": {role: "bad" for role in
                     ("architect", "developer", "reviewer", "fixer")},
        "settings": {},
    }))
    proc = doctor(repo, cfg, "--probe")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "FAIL bad [" in proc.stdout, proc.stdout
    assert "Credit balance is too low" in proc.stdout, proc.stdout


def test_doctor_probe_rejects_empty_output_and_skips_missing_probe(root: Path) -> None:
    repo = make_repo(root)
    cfg = root / "doctor-output.yaml"
    skipped = root / "skipped.txt"
    mixed = root / "mixed.txt"
    import yaml
    shared = agent(f'printf "%s" "$0" > {mixed}')
    cfg.write_text(yaml.safe_dump({
        "agents": {
            "silent": {"command": agent("exit 0") + ["{output}"], "probe": "cheap"},
            "unconfigured": {"command": shared},
            "missing": {"command": agent(f"touch {skipped}")},
            "configured": {"command": shared, "probe": "mixed cheap"},
        },
        "workflow": {"architect": "silent", "developer": "unconfigured",
                     "reviewer": "missing", "fixer": "configured"},
        "settings": {},
    }))
    proc = doctor(repo, cfg, "--probe")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "FAIL silent [" in proc.stdout, proc.stdout
    assert "declares {output} but wrote nothing" in proc.stdout, proc.stdout
    assert "OK   unconfigured, configured [" in proc.stdout, proc.stdout
    assert mixed.read_text() == "mixed cheap"
    assert "SKIP missing (no probe configured)" in proc.stdout, proc.stdout
    assert not skipped.exists(), "agent without a probe was invoked"


def test_doctor_probe_reports_missing_git_without_crashing(root: Path) -> None:
    cfg = root / "doctor-no-git.yaml"
    import yaml
    cfg.write_text(yaml.safe_dump({
        "agents": {"fake": {"command": ["/bin/echo"], "probe": "cheap"}},
        "workflow": {role: "fake" for role in
                     ("architect", "developer", "reviewer", "fixer")},
        "settings": {},
    }))
    proc = subprocess.run(
        [sys.executable, "-m", "stargate", "--config", str(cfg),
         "doctor", "--probe"],
        cwd=root, text=True, capture_output=True,
        env={**os.environ, "PATH": "", "PYTHONPATH": str(ROOT)},
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "SKIP probes (git is required" in proc.stdout, proc.stdout
    assert "Effective settings:" in proc.stdout, proc.stdout
    assert "Traceback" not in proc.stderr, proc.stderr


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


def test_token_cap_stops_the_run(root: Path) -> None:
    """Architect burns the budget; the run must stop before the developer."""
    repo = make_repo(root)
    cfg = root / "h.yaml"
    marker = root / "developer-ran.txt"
    import yaml
    cfg.write_text(yaml.safe_dump({
        "agents": {
            "greedy": {"command": agent('echo "tokens used"; echo "118,089"; echo plan'),
                       "usage_pattern": r"tokens used\s+([\d,]+)"},
            "dev": {"command": agent(f'echo ran > {marker}')},
            "rev": {"command": agent('echo "VERDICT: APPROVED"')},
        },
        "workflow": {"architect": "greedy", "developer": "dev",
                     "reviewer": "rev", "fixer": "dev"},
        "settings": {"max_review_loops": 0, "test_command": "true",
                     "max_task_tokens": 16000},
    }))
    proc = run(repo, cfg)
    assert proc.returncode == 4, proc.stdout + proc.stderr
    assert not marker.exists(), "developer ran despite the budget being spent"
    assert "Token budget reached: 118,089 of 16,000" in proc.stderr, proc.stderr
    assert "BUDGET_EXCEEDED" in proc.stdout, proc.stdout


def test_no_cap_means_no_limit(root: Path) -> None:
    repo = make_repo(root)
    cfg = root / "i.yaml"
    import yaml
    cfg.write_text(yaml.safe_dump({
        "agents": {
            "greedy": {"command": agent('echo "tokens used"; echo "999,999"; echo plan'),
                       "usage_pattern": r"tokens used\s+([\d,]+)"},
            "rev": {"command": agent('echo "VERDICT: APPROVED"')},
        },
        "workflow": {"architect": "greedy", "developer": "greedy",
                     "reviewer": "rev", "fixer": "greedy"},
        "settings": {"max_review_loops": 0, "test_command": "true"},
    }))
    proc = run(repo, cfg)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Tokens:    1,999,998 (no cap)" in proc.stdout, proc.stdout


def test_resume_reuses_plan_and_worktree(root: Path) -> None:
    repo = make_repo(root)
    calls = root / "architect-calls.txt"
    import yaml

    def cfg_for(dev_cmd: list[str], path: Path) -> Path:
        path.write_text(yaml.safe_dump({
            "agents": {
                "arch": {"command": agent(f'echo run >> {calls}; echo "THE PLAN"')},
                "dev": {"command": dev_cmd},
                "rev": {"command": agent('echo "VERDICT: APPROVED"')},
            },
            "workflow": {"architect": "arch", "developer": "dev",
                         "reviewer": "rev", "fixer": "dev"},
            "settings": {"max_review_loops": 0, "test_command": "true"},
        }))
        return path

    broken = cfg_for(agent("echo boom >&2; exit 1"), root / "broken.yaml")
    proc = run(repo, broken)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert calls.read_text().count("run") == 1

    state = json.loads(next((repo / ".stargate" / "runs").glob("*/state.json")).read_text())
    assert state["status"] == "failed", state
    assert state["stage"] == "developer", state
    assert state["completed"] == ["architect", "worktree"], state
    assert "exit code 1" in state["error"], state

    fixed = cfg_for(agent("echo done"), root / "fixed.yaml")
    proc = subprocess.run(
        [sys.executable, "-m", "stargate", "--config", str(fixed),
         "resume", state["run_id"]],
        cwd=repo, text=True, capture_output=True,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert calls.read_text().count("run") == 1, "architect re-ran on resume"
    assert "ARCHITECT (skipped" in proc.stdout, proc.stdout
    assert "Reusing existing worktree" in proc.stdout, proc.stdout


def test_literal_braces_in_a_prompt_survive(root: Path) -> None:
    repo = make_repo(root)
    custom = root / "p"
    custom.mkdir()
    (custom / "architect.md").write_text(
        'Task {task}. Return JSON like {"ok": true} and CSS .a{color:red}.\n'
    )
    dump = root / "arch-prompt.txt"
    cfg = root / "j.yaml"
    write_config(cfg, 'echo "VERDICT: APPROVED"', test_command="true",
                 prompts_dir=str(custom))
    import yaml
    data = yaml.safe_load(cfg.read_text())
    data["agents"]["arch"] = {"command": agent(f'printf "%s" "$0" > {dump}; echo plan')}
    data["workflow"]["architect"] = "arch"
    cfg.write_text(yaml.safe_dump(data))

    proc = run(repo, cfg)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    rendered = dump.read_text()
    assert '{"ok": true}' in rendered, rendered
    assert ".a{color:red}" in rendered, rendered
    assert "Task demo task." in rendered, rendered


def test_prompts_are_frozen_into_the_run(root: Path) -> None:
    """A mid-run reinstall must not take the prompts away from later roles."""
    repo = make_repo(root)
    cfg = root / "k.yaml"
    write_config(cfg, 'echo "VERDICT: APPROVED"', test_command="true")
    proc = run(repo, cfg)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    art = next((repo / ".stargate" / "runs").glob("*"))
    for role in ("architect", "developer", "reviewer", "fixer"):
        assert (art / "prompts" / f"{role}.md").exists(), role
    assert (art / "config.yaml").exists()


def test_slow_agent_prints_a_heartbeat(root: Path) -> None:
    """A silent multi-minute agent must not look like a hang."""
    import stargate.cli
    stargate.cli.HEARTBEAT_SECONDS = 1  # only affects this in-process check

    repo = make_repo(root)
    log = root / "slow.log"
    proc = stargate.cli.run_process(
        ["/bin/sh", "-c", "echo starting; sleep 2.5; echo done"],
        repo, log_path=log, timeout=30,
    )
    assert proc.returncode == 0
    assert "done" in proc.stdout
    assert log.read_text().startswith("starting")


def test_hung_agent_times_out(root: Path) -> None:
    repo = make_repo(root)
    cfg = root / "d.yaml"
    write_config(cfg, "sleep 30", test_command="true", agent_timeout=2)
    proc = run(repo, cfg)
    assert proc.returncode == 1, proc.stdout
    assert "timed out" in proc.stderr, proc.stderr


if __name__ == "__main__":
    for fn in (test_doctor_probe_is_opt_in_and_deduplicated,
               test_doctor_probe_reports_cli_failure,
               test_doctor_probe_rejects_empty_output_and_skips_missing_probe,
               test_doctor_probe_reports_missing_git_without_crashing,
               test_prose_mentioning_approved_is_not_an_approval,
               test_reviewer_receives_test_output,
               test_missing_verdict_is_an_error,
               test_custom_prompts_dir_overrides_one_file,
               test_output_placeholder_forwards_file_not_stdout,
               test_output_placeholder_empty_file_is_an_error,
               test_token_cap_stops_the_run,
               test_no_cap_means_no_limit,
               test_resume_reuses_plan_and_worktree,
               test_literal_braces_in_a_prompt_survive,
               test_prompts_are_frozen_into_the_run,
               test_slow_agent_prints_a_heartbeat,
               test_hung_agent_times_out):
        with tempfile.TemporaryDirectory() as tmp:
            fn(Path(tmp))
        print(f"ok  {fn.__name__}")
    print("\nall good")
