"""Source commands must resolve a task before a run pays for any agent."""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import yaml

from stargate.config import task_sources
from stargate.core import StargateError, slugify
from stargate.source import branch_hint, fetch_task
from tests.harness import agent, doctor, make_repo, run, stargate, write_config


def _sources(*commands: list[str]) -> list[dict]:
    return [{"hosts": ["Tracker.Example", "other.example"], "commands": list(commands)}]


def test_the_first_command_producing_text_wins_without_running_later_commands(root: Path) -> None:
    chosen, later = root / "chosen", root / "later"
    sources = _sources(
        ["cat", str(root / "missing")],
        agent("printf ' \n\t'"),
        agent(f"touch {chosen}; printf 'Task\n\nDetails\n'"),
        agent(f"touch {later}"),
    )
    text = fetch_task({"task_sources": sources}, "https://tracker.example/items/42", root)
    assert text == "Task\n\nDetails\n", repr(text)
    assert chosen.exists()
    assert not later.exists(), "a successful fetch must stop the fallback chain"


def test_every_failed_attempt_reports_its_own_command_and_stderr(root: Path) -> None:
    sources = _sources(
        agent("echo first-error >&2; exit 3"),
        agent("echo empty-error >&2; printf ' \n\t'"),
        agent("echo second-error >&2; exit 4"),
    )
    try:
        fetch_task({"task_sources": sources}, "https://tracker.example/42", root)
    except StargateError as exc:
        error = str(exc)
        assert error.count("  $ ") == 3, error
        assert "exit 3: first-error" in error, error
        assert "exit 4: second-error" in error, error
        assert "empty or whitespace-only output is a failed fetch): empty-error" in error, error
    else:
        raise AssertionError("failed fetches must not become tasks")


def test_missing_or_timed_out_commands_allow_the_next_source_attempt(root: Path) -> None:
    sources = _sources([str(root / "absent-command")], ["slow-command"], ["working-command"])
    with patch("stargate.source.subprocess.run", side_effect=[
        FileNotFoundError("missing binary"),
        subprocess.TimeoutExpired("slow-command", 120, stderr=b"partial error"),
        subprocess.CompletedProcess(["working-command"], 0, "task", ""),
    ]) as invoke:
        assert fetch_task({"task_sources": sources}, "https://tracker.example/42", root) == "task"
    assert invoke.call_count == 3
    with patch("stargate.source.subprocess.run", side_effect=subprocess.TimeoutExpired(
        "slow-command", 120, stderr=b"partial error",
    )):
        try:
            fetch_task({"task_sources": sources}, "https://tracker.example/42", root)
        except StargateError as exc:
            assert "timed out after 120s: partial error" in str(exc), str(exc)
        else:
            raise AssertionError("timeouts must be diagnosed")


def test_configured_hosts_match_case_insensitively_without_matching_the_port(root: Path) -> None:
    config = {"task_sources": _sources(["printf", "%s", "{host}"])}
    for host in ("TRACKER.EXAMPLE", "OTHER.EXAMPLE"):
        text = fetch_task(config, f"https://{host}:8443/items/42", root)
        assert text == host.lower(), text


def test_only_url_host_and_path_expand_without_interpreting_source_values(root: Path) -> None:
    url = "https://tracker.example/unusual/{host}/42/?q={path}#fragment"
    config = {"task_sources": _sources([
        "printf", "%s\n", "{url}", "{host}", "{path}", "{issue}", "{api_url}",
    ])}
    text = fetch_task(config, url, root)
    assert text.splitlines() == [
        url, "tracker.example", "/unusual/{host}/42/", "{issue}", "{api_url}",
    ], text


def test_source_environment_can_override_and_remove_inherited_values(root: Path) -> None:
    sources = _sources(agent('printf "%s:%s" "$SOURCE_SET" "${SOURCE_REMOVE-absent}"'))
    sources[0]["env"] = {"SOURCE_SET": "configured", "SOURCE_REMOVE": None}
    with patch.dict(os.environ, {"SOURCE_SET": "inherited", "SOURCE_REMOVE": "secret"}):
        text = fetch_task({"task_sources": sources}, "https://tracker.example/42", root)
    assert text == "configured:absent", text


def test_invalid_source_shapes_name_the_field_that_needs_repair(root: Path) -> None:
    valid = _sources(["cat", "task.txt"])[0]
    cases = [
        ("command", "task_sources"),
        (["command"], "task_sources[0]"),
        ([{"commands": [["cat"]]}], "hosts"),
        ([{**valid, "hosts": []}], "hosts"),
        ([{**valid, "hosts": [42]}], "hosts"),
        ([{**valid, "hosts": [" "]}], "hosts"),
        ([{**valid, "commands": []}], "commands"),
        ([{**valid, "commands": "cat"}], "commands"),
        ([{**valid, "commands": ["cat", "task.txt"]}], "list of commands"),
        ([{**valid, "commands": [[]]}], "commands[0]"),
        ([{**valid, "commands": [[" "]]}], "executable"),
        ([{**valid, "commands": [["cat", None]]}], "strings or numbers"),
        ([{**valid, "commands": [["cat", {}]]}], "strings or numbers"),
        ([{**valid, "env": []}], "env"),
    ]
    for value, expected in cases:
        try:
            task_sources({"task_sources": value})
        except StargateError as exc:
            assert expected in str(exc), str(exc)
        else:
            raise AssertionError(f"accepted invalid task_sources: {value!r}")
    assert task_sources({}) == task_sources({"task_sources": None}) == []
    numeric = task_sources({"task_sources": _sources(["printf", 42, 1.5])})
    assert numeric[0]["commands"] == [["printf", "42", "1.5"]], numeric


def test_branch_hints_use_the_last_segment_and_unusable_refs_keep_task_naming(root: Path) -> None:
    for suffix, expected in (("/", "demo-task"), ("/items/42//", "42-demo-task"),
                             ("/items/%%%/", "demo-task")):
        hint = branch_hint("https://tracker.example" + suffix + "?query=99#fragment")
        assert slugify(f"{hint} demo task") == expected, hint


def test_a_fetched_task_is_used_verbatim_and_its_origin_is_recorded(root: Path) -> None:
    repo = make_repo(root)
    source, prompt, config = root / "task.txt", root / "prompt.txt", root / "config.yaml"
    task = "  Preserve indentation\n\n    and {literal} details.  \n\n"
    source.write_text(task)
    write_config(config, 'echo "VERDICT: APPROVED"', test_command="true",
                 task_sources=_sources(["cat", str(source)]))
    data = yaml.safe_load(config.read_text())
    data["agents"]["noop"]["command"] = agent(f'printf "%s" "$0" > {prompt}; echo plan')
    config.write_text(yaml.safe_dump(data))
    url = "https://tracker.example/arbitrary/42/"

    proc = run(repo, config, "", "--from", url)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    state_path = next((repo / ".stargate/runs").glob("*/state.json"))
    state = json.loads(state_path.read_text())
    assert state["task"] == task, state
    assert state["task_source"] == url, state
    assert task in prompt.read_text(), prompt.read_text()
    assert state["branch"].startswith("stargate/42-"), state
    summary = (state_path.parent / "summary.md").read_text()
    assert f"Task source: {url}" in summary, summary


def test_failed_fetches_and_bad_config_stop_before_run_artifacts_or_agents(root: Path) -> None:
    repo = make_repo(root)
    config, marker = root / "config.yaml", root / "architect-called"
    cases = [
        ([], "https://UNKNOWN.example/42", "host 'unknown.example'"),
        (_sources(agent("printf ' \n\t'")), "https://tracker.example/42", "failed fetch"),
        (_sources(agent("true")), "https://tracker.example/42", "failed fetch"),
        ([{"hosts": []}], "https://tracker.example/42", "task_sources[0].hosts"),
        (_sources(["cat"]), "relative/path", "absolute URL with a host"),
        (_sources(["cat"]), "https://[broken", "absolute URL with a host"),
        (_sources(["cat"]), "", "absolute URL with a host"),
    ]
    for sources, url, expected in cases:
        write_config(config, 'echo "VERDICT: APPROVED"', test_command="true",
                     task_sources=sources)
        data = yaml.safe_load(config.read_text())
        data["agents"]["noop"]["command"] = agent(f"touch {marker}; echo plan")
        config.write_text(yaml.safe_dump(data))
        proc = run(repo, config, "", "--from", url)
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert expected in proc.stderr, proc.stderr
        assert not (repo / ".stargate/runs").exists(), proc.stdout
        assert not marker.exists(), "fetch failure must not pay for an architect"


def test_task_and_from_conflict_before_config_loading_or_run_reservation(root: Path) -> None:
    repo = make_repo(root)
    missing_config = root / "not-even-loaded.yaml"
    proc = run(repo, missing_config, "typed task", "--from", "https://tracker.example/42")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "--from cannot be combined with a task argument" in proc.stderr, proc.stderr
    proc = run(repo, missing_config, "")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "a task description or --from URL is required" in proc.stderr, proc.stderr
    assert not (repo / ".stargate/runs").exists(), proc.stdout


def test_resume_reuses_the_frozen_task_when_the_source_is_gone(root: Path) -> None:
    repo = make_repo(root)
    source, calls, ready = root / "task.txt", root / "source-calls", root / "ready"
    config = root / "config.yaml"
    task = "Frozen task\n\nKeep this after the source disappears.\n"
    source.write_text(task)
    write_config(config, 'echo "VERDICT: APPROVED"', test_command="true",
                 task_sources=_sources(agent(f"echo called >> {calls}; cat {source}")))
    data = yaml.safe_load(config.read_text())
    data["agents"]["dev"]["command"] = agent(
        f"test -f {ready} || exit 1; echo change >> impl.txt; echo done"
    )
    config.write_text(yaml.safe_dump(data))
    url = "https://tracker.example/42"
    proc = run(repo, config, "", "--from", url)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    state_path = next((repo / ".stargate/runs").glob("*/state.json"))
    state = json.loads(state_path.read_text())
    assert state["stage"] == "developer", state
    source.unlink()
    config.unlink()
    ready.touch()

    proc = stargate(repo, "resume", state["run_id"], config_home=root / "config-home")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert calls.read_text() == "called\n", "resume must not re-fetch"
    resumed = json.loads(state_path.read_text())
    assert resumed["task"] == task and resumed["task_source"] == url, resumed
    assert "ARCHITECT (skipped" in proc.stdout, proc.stdout
    summary = (state_path.parent / "summary.md").read_text()
    assert f"Task source: {url}" in summary, summary


def test_doctor_exposes_source_binaries_without_fetching_or_printing_env_values(root: Path) -> None:
    repo = make_repo(root)
    config = root / "config.yaml"
    missing = "stargate-test-missing-source-binary"
    sources = _sources(["cat", "{path}"], [missing, "{url}"])
    sources[0]["env"] = {"SOURCE_TOKEN": "secret-value", "SOURCE_REMOVE": None}
    write_config(config, 'echo "VERDICT: APPROVED"', test_command="true", task_sources=sources)
    proc = doctor(repo, config)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "Task sources:" in proc.stdout, proc.stdout
    assert "tracker.example, other.example" in proc.stdout, proc.stdout
    assert "FOUND    cat" in proc.stdout, proc.stdout
    assert f"MISSING  {missing}" in proc.stdout, proc.stdout
    assert "SOURCE_TOKEN" in proc.stdout and "SOURCE_REMOVE (unset)" in proc.stdout, proc.stdout
    assert "secret-value" not in proc.stdout, proc.stdout
    assert "{url}" in proc.stdout and "{path}" in proc.stdout, proc.stdout
    assert not (repo / ".stargate/runs").exists(), proc.stdout


def test_doctor_rejects_invalid_sources_before_probing_any_agent(root: Path) -> None:
    repo = make_repo(root)
    config = root / "config.yaml"
    write_config(config, 'echo "VERDICT: APPROVED"', test_command="true",
                 task_sources=[{"hosts": ["tracker.example"], "commands": ["cat"]}])
    marker = root / "probe-called"
    data = yaml.safe_load(config.read_text())
    data["agents"]["noop"] = {
        "command": agent(f"touch {marker}; echo done"), "probe": "Check the agent",
    }
    config.write_text(yaml.safe_dump(data))
    for args in ((), ("--probe",)):
        proc = doctor(repo, config, *args)
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "ERROR" in proc.stdout, proc.stdout
        assert "task_sources[0].commands[0]" in proc.stdout, proc.stdout
        assert not marker.exists(), "invalid source config must fail before any agent is called"
