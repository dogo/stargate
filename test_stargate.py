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
import time
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
            "dev": {"command": agent("echo change >> impl.txt; echo done")},
            "reviewer": {"command": agent(reviewer)},
        },
        "workflow": {"architect": "noop", "developer": "dev",
                     "reviewer": "reviewer", "fixer": "dev"},
        "settings": {"max_review_loops": loops, "test_command": test_command,
                     "agent_timeout_seconds": agent_timeout, "test_timeout_seconds": 30,
                     "prompts_dir": prompts_dir},
    }
    path.write_text(yaml.safe_dump(cfg))


def run(
    repo: Path, config: Path, task: str = "demo task"
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "stargate", "--config", str(config), "run", task],
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


def runs(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "stargate", "runs"],
        cwd=repo, text=True, capture_output=True,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )


def stargate(
    repo: Path, *args: str, config_home: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "stargate", *args],
        cwd=repo, text=True, capture_output=True,
        env={**os.environ, "PYTHONPATH": str(ROOT),
             "XDG_CONFIG_HOME": str(config_home)},
    )


def test_settings_only_project_config_is_valid(root: Path) -> None:
    repo = make_repo(root)
    (repo / ".stargate.yaml").write_text(
        'settings:\n  test_command: "npm test"\n'
    )

    proc = stargate(repo, "doctor", config_home=root / "empty-config")
    assert "Config must contain" not in proc.stderr, proc.stderr
    assert "ERROR" not in proc.stderr, proc.stderr
    assert "Effective settings:" in proc.stdout, proc.stdout
    assert "'npm test'" in proc.stdout, proc.stdout


def test_project_config_layers_over_user_config(root: Path) -> None:
    repo = make_repo(root)
    config_home = root / "config"
    user_cfg = config_home / "stargate" / "agents.yaml"
    user_cfg.parent.mkdir(parents=True)
    reviewer_prompt = root / "reviewer-prompt.txt"
    write_config(
        user_cfg,
        f'printf "%s" "$0" > {reviewer_prompt}; echo "VERDICT: APPROVED"',
        test_command="false",
        agent_timeout=73,
    )
    (repo / ".stargate.yaml").write_text(
        "settings:\n"
        "  test_command: echo MARKER_PROJECT_TESTS\n"
        "  max_review_loops: 0\n"
    )

    proc = stargate(
        repo, "run", "demo task", config_home=config_home
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "MARKER_PROJECT_TESTS" in reviewer_prompt.read_text()

    import yaml
    artifacts = next((repo / ".stargate" / "runs").glob("*"))
    frozen = yaml.safe_load((artifacts / "config.yaml").read_text())
    assert frozen["settings"]["test_command"] == "echo MARKER_PROJECT_TESTS"
    assert frozen["settings"]["agent_timeout_seconds"] == 73
    assert frozen["workflow"]["architect"] == "noop"
    assert "noop" in frozen["agents"]
    assert "developer" in frozen["agents"], "packaged base was not frozen"


def test_project_config_overrides_one_agent_only(root: Path) -> None:
    repo = make_repo(root)
    config_home = root / "config"
    user_cfg = config_home / "stargate" / "agents.yaml"
    user_cfg.parent.mkdir(parents=True)
    inherited = root / "inherited-agent.txt"
    project = root / "project-agent.txt"

    import yaml
    user_cfg.write_text(yaml.safe_dump({
        "agents": {
            "noop": {"command": agent(f"echo inherited >> {inherited}; echo done")},
            "dev": {"command": agent("echo change >> impl.txt; echo done")},
            "reviewer": {"command": agent('echo "VERDICT: APPROVED"')},
        },
        "workflow": {"architect": "noop", "developer": "dev",
                     "reviewer": "reviewer", "fixer": "dev"},
        "settings": {"max_review_loops": 0, "test_command": "true"},
    }))
    (repo / ".stargate.yaml").write_text(yaml.safe_dump({
        "agents": {"project_reviewer": {"command": agent(
            f'echo project > {project}; echo "VERDICT: APPROVED"'
        )}},
        "workflow": {"reviewer": "project_reviewer"},
    }))

    proc = stargate(
        repo, "run", "demo task", config_home=config_home
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert project.exists(), "project reviewer did not run"
    assert inherited.exists(), "agents omitted by the project were not inherited"


def test_explicit_config_is_not_layered(root: Path) -> None:
    repo = make_repo(root)
    leak = root / "project-setting-leaked.txt"
    (repo / ".stargate.yaml").write_text(
        f"settings:\n  test_command: touch {leak}\n"
    )
    cfg = root / "explicit.yaml"
    write_config(
        cfg, 'echo "VERDICT: APPROVED"', test_command="true"
    )

    proc = run(repo, cfg)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not leak.exists(), "project config was layered over explicit --config"


def test_doctor_reports_config_provenance(root: Path) -> None:
    repo = make_repo(root)
    config_home = root / "config"
    user_cfg = config_home / "stargate" / "agents.yaml"
    user_cfg.parent.mkdir(parents=True)
    write_config(
        user_cfg, 'echo "VERDICT: APPROVED"', test_command="false",
        agent_timeout=73,
    )
    project_cfg = repo / ".stargate.yaml"
    project_cfg.write_text("settings:\n  test_command: echo PROJECT\n")

    proc = stargate(repo, "doctor", config_home=config_home)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Config sources (most specific first):" in proc.stdout
    assert f"[1] {project_cfg}" in proc.stdout
    assert f"[2] {user_cfg}" in proc.stdout
    assert "[3]" in proc.stdout and "(packaged defaults)" in proc.stdout
    test_line = next(line for line in proc.stdout.splitlines()
                     if line.strip().startswith("test_command"))
    timeout_line = next(line for line in proc.stdout.splitlines()
                        if line.strip().startswith("agent_timeout_seconds"))
    packaged_line = next(line for line in proc.stdout.splitlines()
                         if line.strip().startswith("max_task_tokens"))
    assert test_line.endswith("[1]"), test_line
    assert timeout_line.endswith("[2]"), timeout_line
    assert packaged_line.endswith("[3]"), packaged_line


def test_sigterm_records_a_terminal_status(root: Path) -> None:
    repo = make_repo(root)
    cfg = root / "signal.yaml"
    started = root / "agent-started.txt"
    finished = root / "agent-finished.txt"

    import yaml
    # The cleanup contract covers the process Popen starts; avoiding a shell
    # here keeps the regression test from implying recursive process cleanup.
    slow = (
        "import os, pathlib, time; "
        f"pathlib.Path({str(started)!r}).write_text(str(os.getpid())); "
        "time.sleep(30); "
        f"pathlib.Path({str(finished)!r}).write_text('done')"
    )
    cfg.write_text(yaml.safe_dump({
        "agents": {
            "slow": {"command": [sys.executable, "-c", slow]},
            "noop": {"command": agent("echo done")},
            "reviewer": {"command": agent('echo "VERDICT: APPROVED"')},
        },
        "workflow": {"architect": "slow", "developer": "noop",
                     "reviewer": "reviewer", "fixer": "noop"},
        "settings": {"max_review_loops": 0, "test_command": "true",
                     "agent_timeout_seconds": 60},
    }))
    proc = subprocess.Popen(
        [sys.executable, "-m", "stargate", "--config", str(cfg),
         "run", "signal test"],
        cwd=repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )

    deadline = time.monotonic() + 15
    while not started.exists() and proc.poll() is None \
            and time.monotonic() < deadline:
        time.sleep(0.05)
    if not started.exists():
        proc.kill()
        out, err = proc.communicate(timeout=10)
        raise AssertionError(f"agent did not start\nstdout:\n{out}\nstderr:\n{err}")

    agent_pid = int(started.read_text())
    proc.terminate()
    out, err = proc.communicate(timeout=15)
    assert proc.returncode == 143, out + err
    assert "Resume with: stargate resume" in err, err

    state_path = next((repo / ".stargate" / "runs").glob("*/state.json"))
    state = json.loads(state_path.read_text())
    assert state["status"] == "failed", state
    assert "signal 15" in state["error"], state

    listing = runs(repo)
    run_line = next(line for line in listing.stdout.splitlines()
                    if state["run_id"] in line)
    assert "failed" in run_line and "running" not in run_line, run_line

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(agent_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        raise AssertionError(f"agent process {agent_pid} survived SIGTERM cleanup")
    assert not finished.exists(), "agent continued after the orchestrator exited"


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
            "dev": {"command": agent("echo change >> impl.txt; echo done")},
            "rev": {"command": agent(f'printf "%s" "$0" > {dump}; echo "VERDICT: APPROVED"')},
        },
        "workflow": {"architect": "arch", "developer": "dev",
                     "reviewer": "rev", "fixer": "dev"},
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
            "dev": {"command": agent(
                'echo change >> impl.txt; echo "tokens used"; echo "999,999"; echo done'
            ), "usage_pattern": r"tokens used\s+([\d,]+)"},
            "rev": {"command": agent('echo "VERDICT: APPROVED"')},
        },
        "workflow": {"architect": "greedy", "developer": "dev",
                     "reviewer": "rev", "fixer": "dev"},
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

    fixed = cfg_for(
        agent("echo change >> impl.txt; echo done"), root / "fixed.yaml"
    )
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


def test_agent_env_sets_and_unsets(root: Path) -> None:
    repo = make_repo(root)
    seen = root / "seen.txt"
    cfg = root / "env.yaml"
    import yaml
    cfg.write_text(yaml.safe_dump({
        "agents": {
            "arch": {
                "command": agent(f'echo "KEY=[$STARGATE_KEY] INHERITED=[$STARGATE_INHERITED]" > {seen}; echo plan'),
                "env": {"STARGATE_KEY": "from-config", "STARGATE_INHERITED": None},
            },
            "noop": {"command": agent("echo done")},
            "dev": {"command": agent("echo change >> impl.txt; echo done")},
            "rev": {"command": agent('echo "VERDICT: APPROVED"')},
        },
        "workflow": {"architect": "arch", "developer": "dev",
                     "reviewer": "rev", "fixer": "dev"},
        "settings": {"max_review_loops": 0, "test_command": "true"},
    }))
    proc = subprocess.run(
        [sys.executable, "-m", "stargate", "--config", str(cfg), "run", "demo task"],
        cwd=repo, text=True, capture_output=True,
        env={**os.environ, "PYTHONPATH": str(ROOT),
             "STARGATE_INHERITED": "leaked-from-orchestrator"},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert seen.read_text().strip() == "KEY=[from-config] INHERITED=[]", seen.read_text()


def test_probe_dedup_separates_agents_by_env(root: Path) -> None:
    """Same command, different credentials: two things to verify, not one."""
    repo = make_repo(root)
    calls = root / "probe-calls.txt"
    cfg = root / "env-probe.yaml"
    import yaml
    same = agent(f'echo "$STARGATE_WHO" >> {calls}; echo OK')
    cfg.write_text(yaml.safe_dump({
        "agents": {
            "a": {"command": same, "probe": "cheap", "env": {"STARGATE_WHO": "first"}},
            "b": {"command": same, "probe": "cheap", "env": {"STARGATE_WHO": "second"}},
        },
        "workflow": {"architect": "a", "reviewer": "a",
                     "developer": "b", "fixer": "b"},
        "settings": {},
    }))
    proc = doctor(repo, cfg, "--probe")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert sorted(calls.read_text().split()) == ["first", "second"], calls.read_text()
    assert "env: STARGATE_WHO" in proc.stdout, proc.stdout


def test_env_values_are_never_printed(root: Path) -> None:
    repo = make_repo(root)
    cfg = root / "secret.yaml"
    import yaml
    cfg.write_text(yaml.safe_dump({
        "agents": {"a": {"command": agent("echo ok"),
                         "env": {"MY_TOKEN": "sk-super-secret-value"}}},
        "workflow": {role: "a" for role in
                     ("architect", "developer", "reviewer", "fixer")},
        "settings": {},
    }))
    proc = doctor(repo, cfg)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "MY_TOKEN" in proc.stdout, proc.stdout
    assert "sk-super-secret-value" not in proc.stdout, "env value leaked into doctor"


def test_hung_agent_times_out(root: Path) -> None:
    repo = make_repo(root)
    cfg = root / "d.yaml"
    write_config(cfg, "sleep 30", test_command="true", agent_timeout=2)
    proc = run(repo, cfg)
    assert proc.returncode == 1, proc.stdout
    assert "timed out" in proc.stderr, proc.stderr


def test_retry_recovers_a_transient_failure(root: Path) -> None:
    repo = make_repo(root)
    cfg = root / "retry.yaml"
    calls = root / "architect-calls.txt"
    import yaml
    transient = agent(
        f'if test ! -e {calls}; then echo first > {calls}; '
        'echo boom >&2; exit 1; fi; echo "THE PLAN"'
    )
    cfg.write_text(yaml.safe_dump({
        "agents": {
            "arch": {"command": transient},
            "noop": {"command": agent("echo done")},
            "dev": {"command": agent("echo change >> impl.txt; echo done")},
            "rev": {"command": agent('echo "VERDICT: APPROVED"')},
        },
        "workflow": {"architect": "arch", "developer": "dev",
                     "reviewer": "rev", "fixer": "dev"},
        "settings": {"max_review_loops": 0, "test_command": "true",
                     "agent_retries": 2, "agent_retry_backoff_seconds": 0},
    }))

    proc = run(repo, cfg)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "attempt 1 of 3 failed" in proc.stdout, proc.stdout
    assert "retrying in 0s (attempt 2 of 3)" in proc.stdout, proc.stdout
    artifacts = next((repo / ".stargate" / "runs").glob("*"))
    assert (artifacts / "plan.md.log").read_text().strip() == "boom"
    assert (artifacts / "plan.md.attempt-2.log").exists()


def test_retries_exhausted_fails_exactly_like_today(root: Path) -> None:
    repo = make_repo(root)
    cfg = root / "retry-exhausted.yaml"
    state_counter = root / "state-counter.txt"
    calls = root / "calls.txt"
    import yaml
    always_different = agent(
        f'echo call >> {calls}; value=$(cat {state_counter} 2>/dev/null || true); '
        'case "$value" in "") next=a;; a) next=b;; *) next=c;; esac; '
        f'echo "$next" > {state_counter}; echo "boom $next" >&2; exit 1'
    )
    cfg.write_text(yaml.safe_dump({
        "agents": {"bad": {"command": always_different}},
        "workflow": {role: "bad" for role in
                     ("architect", "developer", "reviewer", "fixer")},
        "settings": {"agent_retries": 2, "agent_retry_backoff_seconds": 0},
    }))

    proc = run(repo, cfg)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert len(calls.read_text().splitlines()) == 3, calls.read_text()
    assert "Command failed with exit code 1" in proc.stderr, proc.stderr
    assert "Resume with: stargate resume" in proc.stderr, proc.stderr
    state_path = next((repo / ".stargate" / "runs").glob("*/state.json"))
    assert json.loads(state_path.read_text())["status"] == "failed"


def test_identical_failure_stops_retrying_early(root: Path) -> None:
    repo = make_repo(root)
    cfg = root / "retry-identical.yaml"
    calls = root / "calls.txt"
    import yaml
    permanent = agent(f'echo call >> {calls}; echo permanent >&2; exit 1')
    cfg.write_text(yaml.safe_dump({
        "agents": {"bad": {"command": permanent}},
        "workflow": {role: "bad" for role in
                     ("architect", "developer", "reviewer", "fixer")},
        "settings": {"agent_retries": 5, "agent_retry_backoff_seconds": 0},
    }))

    proc = run(repo, cfg)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert len(calls.read_text().splitlines()) == 2, calls.read_text()
    assert "failed identically twice; not retrying 4 more time(s)" in proc.stdout
    assert "Command failed with exit code 1" in proc.stderr, proc.stderr


def test_retry_counts_tokens_once_per_attempt(root: Path) -> None:
    repo = make_repo(root)
    cfg = root / "retry-tokens.yaml"
    calls = root / "architect-calls.txt"
    import yaml
    metered = agent(
        'echo "tokens used"; echo "1,000"; '
        f'if test ! -e {calls}; then echo first > {calls}; exit 1; fi; '
        'echo plan'
    )
    cfg.write_text(yaml.safe_dump({
        "agents": {
            "arch": {"command": metered,
                     "usage_pattern": r"tokens used\s+([\d,]+)"},
            "noop": {"command": agent("echo done")},
            "dev": {"command": agent("echo change >> impl.txt; echo done")},
            "rev": {"command": agent('echo "VERDICT: APPROVED"')},
        },
        "workflow": {"architect": "arch", "developer": "dev",
                     "reviewer": "rev", "fixer": "dev"},
        "settings": {"max_review_loops": 0, "test_command": "true",
                     "agent_retries": 1, "agent_retry_backoff_seconds": 0},
    }))

    proc = run(repo, cfg)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Tokens:    2,000 (no cap)" in proc.stdout, proc.stdout


def test_no_retries_by_default(root: Path) -> None:
    repo = make_repo(root)
    cfg = root / "no-retry.yaml"
    calls = root / "developer-calls.txt"
    import yaml
    cfg.write_text(yaml.safe_dump({
        "agents": {
            "arch": {"command": agent("echo plan")},
            "bad": {"command": agent(
                f'echo call >> {calls}; echo boom >&2; exit 1'
            )},
            "rev": {"command": agent('echo "VERDICT: APPROVED"')},
        },
        "workflow": {"architect": "arch", "developer": "bad",
                     "reviewer": "rev", "fixer": "bad"},
        "settings": {"max_review_loops": 0, "test_command": "true"},
    }))

    proc = run(repo, cfg)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert len(calls.read_text().splitlines()) == 1, calls.read_text()
    assert "attempt 1 of" not in proc.stdout, proc.stdout


def test_runs_lists_newest_first_and_marks_resumable(root: Path) -> None:
    repo = make_repo(root)
    approved = root / "approved.yaml"
    failed = root / "failed.yaml"
    write_config(
        approved, 'echo "VERDICT: APPROVED"', test_command="true"
    )
    first = run(repo, approved, "older approved task")
    assert first.returncode == 0, first.stdout + first.stderr

    import yaml
    failed.write_text(yaml.safe_dump({
        "agents": {"bad": {"command": agent("echo boom >&2; exit 1")}},
        "workflow": {role: "bad" for role in
                     ("architect", "developer", "reviewer", "fixer")},
        "settings": {},
    }))
    second = run(repo, failed, "newer failed task")
    assert second.returncode == 1, second.stdout + second.stderr

    states = [json.loads(path.read_text()) for path in
              (repo / ".stargate" / "runs").glob("*/state.json")]
    approved_id = next(state["run_id"] for state in states
                       if state["status"] == "approved")
    failed_id = next(state["run_id"] for state in states
                     if state["status"] == "failed")
    proc = runs(repo)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    failed_line = next(line for line in proc.stdout.splitlines()
                       if failed_id in line)
    approved_line = next(line for line in proc.stdout.splitlines()
                         if approved_id in line)
    assert failed_line.startswith("* "), failed_line
    assert approved_line.startswith("  "), approved_line
    assert f"Resume the newest with: stargate resume {failed_id}" in proc.stdout
    assert proc.stdout.index(failed_id) < proc.stdout.index(approved_id), proc.stdout
    assert "Traceback" not in proc.stdout + proc.stderr


def test_runs_without_a_stargate_directory(root: Path) -> None:
    repo = make_repo(root)
    proc = runs(repo)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "No runs recorded in" in proc.stdout, proc.stdout
    assert "Traceback" not in proc.stdout + proc.stderr
    assert not (repo / ".stargate").exists(), "listing created .stargate"

    (repo / ".stargate" / "runs").mkdir(parents=True)
    empty = runs(repo)
    assert empty.returncode == 0, empty.stdout + empty.stderr
    assert "No runs recorded in" in empty.stdout, empty.stdout


def test_runs_survives_a_corrupt_state_file(root: Path) -> None:
    repo = make_repo(root)
    run_root = repo / ".stargate" / "runs"
    corrupt = run_root / "20260831-120000-corrupt"
    missing = run_root / "20260831-110000-missing"
    corrupt.mkdir(parents=True)
    missing.mkdir()
    state_path = corrupt / "state.json"
    state_path.write_bytes(b"not json {{{\n")
    before_bytes = state_path.read_bytes()
    before_entries = {
        path.name: sorted(child.name for child in path.iterdir())
        for path in (corrupt, missing)
    }

    proc = runs(repo)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Traceback" not in proc.stdout + proc.stderr
    assert corrupt.name in proc.stdout, proc.stdout
    assert missing.name in proc.stdout, proc.stdout
    assert "state.json missing or unreadable" in proc.stdout, proc.stdout
    assert state_path.read_bytes() == before_bytes
    after_entries = {
        path.name: sorted(child.name for child in path.iterdir())
        for path in (corrupt, missing)
    }
    assert after_entries == before_entries, "listing modified a run directory"


def test_doctor_probe_verifies_the_capability_a_role_uses(root: Path) -> None:
    repo = make_repo(root)
    cfg = root / "capability-probes.yaml"
    import yaml
    cfg.write_text(yaml.safe_dump({
        "agents": {
            "read_ok": {
                "command": agent('cat "$0"'),
                "probe": "{probe_file}",
                "probe_expect": "read",
            },
            "read_broken": {
                "command": agent("echo OK; echo reader"),
                "probe": "{probe_file}",
                "probe_expect": "read",
            },
            "write_ok": {
                "command": agent('echo OK > "$0"'),
                "probe": "{probe_file}",
                "probe_expect": "write",
            },
            "write_broken": {
                "command": agent("echo OK; echo writer"),
                "probe": "{probe_file}",
                "probe_expect": "write",
            },
        },
        "workflow": {
            "architect": "read_ok",
            "developer": "write_ok",
            "reviewer": "read_broken",
            "fixer": "write_broken",
        },
        "settings": {},
    }))

    proc = doctor(repo, cfg, "--probe")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "OK   read_ok (read) [" in proc.stdout, proc.stdout
    assert "FAIL read_broken (read) [" in proc.stdout, proc.stdout
    assert "did not return the marker" in proc.stdout, proc.stdout
    assert "OK   write_ok (write) [" in proc.stdout, proc.stdout
    assert "FAIL write_broken (write) [" in proc.stdout, proc.stdout
    assert "did not write" in proc.stdout, proc.stdout
    assert not list(repo.glob("probe-*.txt")), "probe file reached the user's repo"
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, text=True,
        capture_output=True, check=True,
    )
    assert not status.stdout, status.stdout


def test_developer_that_changes_nothing_stops_the_run(root: Path) -> None:
    repo = make_repo(root)
    cfg = root / "noop-developer.yaml"
    reviewed = root / "reviewed.txt"
    import yaml
    cfg.write_text(yaml.safe_dump({
        "agents": {
            "arch": {"command": agent("echo plan")},
            "dev": {"command": agent("echo done")},
            "rev": {"command": agent(
                f'echo reviewed > {reviewed}; echo "VERDICT: APPROVED"'
            )},
        },
        "workflow": {"architect": "arch", "developer": "dev",
                     "reviewer": "rev", "fixer": "dev"},
        "settings": {"max_review_loops": 0, "test_command": "true"},
    }))

    proc = run(repo, cfg)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "changed nothing" in proc.stderr, proc.stderr
    assert not reviewed.exists(), "reviewer ran after an empty developer stage"
    state_path = next((repo / ".stargate" / "runs").glob("*/state.json"))
    state = json.loads(state_path.read_text())
    assert state["status"] == "failed", state
    assert state["stage"] == "developer", state
    assert state["completed"] == ["architect", "worktree"], state


def test_untracked_new_file_counts_as_work(root: Path) -> None:
    repo = make_repo(root)
    cfg = root / "untracked-developer.yaml"
    import yaml
    cfg.write_text(yaml.safe_dump({
        "agents": {
            "arch": {"command": agent("echo plan")},
            "dev": {"command": agent("echo new > newfile.txt; echo done")},
            "rev": {"command": agent('echo "VERDICT: APPROVED"')},
        },
        "workflow": {"architect": "arch", "developer": "dev",
                     "reviewer": "rev", "fixer": "dev"},
        "settings": {"max_review_loops": 0, "test_command": "true"},
    }))

    proc = run(repo, cfg)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    state_path = next((repo / ".stargate" / "runs").glob("*/state.json"))
    state = json.loads(state_path.read_text())
    assert (Path(state["worktree"]) / "newfile.txt").read_text() == "new\n"


def test_resume_redo_reruns_a_completed_stage(root: Path) -> None:
    repo = make_repo(root)
    architect_calls = root / "architect-calls.txt"
    developer_calls = root / "developer-calls.txt"
    import yaml

    def config(path: Path, reviewer: str) -> Path:
        path.write_text(yaml.safe_dump({
            "agents": {
                "arch": {"command": agent(
                    f'echo run >> {architect_calls}; echo "THE PLAN"'
                )},
                "dev": {"command": agent(
                    f"echo run >> {developer_calls}; "
                    "echo change >> impl.txt; echo done"
                )},
                "rev": {"command": agent(reviewer)},
            },
            "workflow": {"architect": "arch", "developer": "dev",
                         "reviewer": "rev", "fixer": "dev"},
            "settings": {"max_review_loops": 0, "test_command": "true"},
        }))
        return path

    broken = config(root / "redo-broken.yaml", "echo no-verdict")
    first = run(repo, broken)
    assert first.returncode == 1, first.stdout + first.stderr
    state_path = next((repo / ".stargate" / "runs").glob("*/state.json"))
    state = json.loads(state_path.read_text())
    assert "developer" in state["completed"], state

    fixed = config(root / "redo-fixed.yaml", 'echo "VERDICT: APPROVED"')
    resumed = subprocess.run(
        [sys.executable, "-m", "stargate", "--config", str(fixed),
         "resume", state["run_id"], "--redo", "developer"],
        cwd=repo, text=True, capture_output=True,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert len(developer_calls.read_text().splitlines()) == 2
    assert len(architect_calls.read_text().splitlines()) == 1
    assert "DEVELOPER (skipped" not in resumed.stdout, resumed.stdout
    assert "ARCHITECT (skipped" in resumed.stdout, resumed.stdout


def test_fixer_that_changes_nothing_stops_the_review_loop(root: Path) -> None:
    repo = make_repo(root)
    cfg = root / "noop-fixer.yaml"
    reviewer_calls = root / "reviewer-calls.txt"
    fixer_calls = root / "fixer-calls.txt"
    test_calls = root / "test-calls.txt"
    import yaml
    cfg.write_text(yaml.safe_dump({
        "agents": {
            "arch": {"command": agent("echo plan")},
            "dev": {"command": agent("echo change >> impl.txt; echo done")},
            "rev": {"command": agent(
                f'echo review >> {reviewer_calls}; '
                'echo "VERDICT: CHANGES_REQUESTED"'
            )},
            "fix": {"command": agent(
                f"echo fix >> {fixer_calls}; echo no-change-needed"
            )},
        },
        "workflow": {"architect": "arch", "developer": "dev",
                     "reviewer": "rev", "fixer": "fix"},
        "settings": {
            "max_review_loops": 2,
            "test_command": f"echo test >> {test_calls}",
        },
    }))

    proc = run(repo, cfg)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert len(reviewer_calls.read_text().splitlines()) == 1
    assert len(fixer_calls.read_text().splitlines()) == 1
    assert len(test_calls.read_text().splitlines()) == 1
    assert "changed nothing" in proc.stderr, proc.stderr
    assert "Verdict:   CHANGES_REQUESTED" in proc.stdout, proc.stdout


if __name__ == "__main__":
    for fn in (test_settings_only_project_config_is_valid,
               test_project_config_layers_over_user_config,
               test_project_config_overrides_one_agent_only,
               test_explicit_config_is_not_layered,
               test_doctor_reports_config_provenance,
               test_sigterm_records_a_terminal_status,
               test_doctor_probe_is_opt_in_and_deduplicated,
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
               test_agent_env_sets_and_unsets,
               test_probe_dedup_separates_agents_by_env,
               test_env_values_are_never_printed,
               test_hung_agent_times_out,
               test_retry_recovers_a_transient_failure,
               test_retries_exhausted_fails_exactly_like_today,
               test_identical_failure_stops_retrying_early,
               test_retry_counts_tokens_once_per_attempt,
               test_no_retries_by_default,
               test_runs_lists_newest_first_and_marks_resumable,
               test_runs_without_a_stargate_directory,
               test_runs_survives_a_corrupt_state_file,
               test_doctor_probe_verifies_the_capability_a_role_uses,
               test_developer_that_changes_nothing_stops_the_run,
               test_untracked_new_file_counts_as_work,
               test_resume_redo_reruns_a_completed_stage,
               test_fixer_that_changes_nothing_stops_the_review_loop):
        with tempfile.TemporaryDirectory() as tmp:
            fn(Path(tmp))
        print(f"ok  {fn.__name__}")
    print("\nall good")
