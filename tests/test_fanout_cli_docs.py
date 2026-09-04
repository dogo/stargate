"""Regression coverage for the shipped fan-out CLI and prompt contract."""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from stargate.cli import _saved_run_mode
from stargate.core import StargateError
from stargate.fanout import parse_task_graph
from tests.harness import ROOT, doctor, make_repo, run, stargate, write_config


def compact(text: str) -> str:
    return " ".join(re.sub(r"-\s*\n\s*", "-", text).split())


def test_fanout_prompt_matches_parser_contract(root: Path) -> None:
    del root
    prompt = (ROOT / "stargate" / "prompts" / "fanout.md").read_text()
    task_id = "a" * 48
    prompt_compliant_graph = json.dumps(
        {
            "name": "prompt contract",
            "tasks": [
                {
                    "id": task_id,
                    "task": "Implement the contract.",
                    "depends_on": [],
                    "acceptance": ["The contract is implemented."],
                },
                {
                    "id": "verify",
                    "task": "Verify the implementation.",
                    "depends_on": [task_id],
                },
            ],
        }
    )

    assert "one bare JSON object" in prompt
    assert "[a-z0-9][a-z0-9-]{0,47}" in prompt
    assert "`depends_on` is optional and defaults to `[]`" in prompt
    assert "`acceptance` is optional and defaults to `[]`" in prompt
    assert "of non-empty strings; the list itself may be empty" in compact(prompt)
    name, tasks = parse_task_graph(prompt_compliant_graph, max_tasks=2)
    assert name == "prompt contract"
    assert tasks[0].id == task_id
    assert tasks[0].depends_on == ()
    assert tasks[0].acceptance == ("The contract is implemented.",)
    assert tasks[1].depends_on == (task_id,)
    assert tasks[1].acceptance == ()
    explicit_empty = json.loads(prompt_compliant_graph)
    explicit_empty["tasks"][1]["acceptance"] = []
    assert parse_task_graph(json.dumps(explicit_empty), max_tasks=2)[1][1].acceptance == ()

    try:
        parse_task_graph(
            f"```json\n{prompt_compliant_graph}\n```", max_tasks=2
        )
    except StargateError as exc:
        assert "valid JSON" in str(exc)
    else:
        raise AssertionError("a fenced JSON response was accepted")


def test_saved_run_mode_migrates_only_modeless_state_to_linear(root: Path) -> None:
    repo = make_repo(root)
    runs = repo / ".stargate" / "runs"

    for run_id, state in (
        ("unknown", {"mode": "parallel"}),
        ("null", {"mode": None}),
        ("non-object", []),
    ):
        artifacts = runs / run_id
        artifacts.mkdir(parents=True)
        (artifacts / "state.json").write_text(json.dumps(state))
        assert _saved_run_mode(repo, run_id) is None

    modeless = runs / "mode-less"
    modeless.mkdir(parents=True)
    (modeless / "state.json").write_text("{}\n")
    linear = runs / "linear"
    linear.mkdir(parents=True)
    (linear / "state.json").write_text(json.dumps({"mode": "linear"}))
    fanout = runs / "fanout"
    fanout.mkdir(parents=True)
    (fanout / "state.json").write_text(json.dumps({"mode": "fanout"}))
    assert _saved_run_mode(repo, "mode-less") == "linear"
    assert _saved_run_mode(repo, "linear") == "linear"
    assert _saved_run_mode(repo, "fanout") == "fanout"


def test_readme_describes_resumable_fanout_budget_stop(root: Path) -> None:
    del root
    readme = (ROOT / "README.md").read_text()
    compact_readme = compact(readme)

    assert "also prints a RESULT block and writes `summary.md`" in readme
    assert "without an integration terminal commit" in readme
    assert "does not create a terminal integration commit" in compact_readme
    assert "without a RESULT block, `summary.md`, or terminal commit" not in readme
    assert "does not create `summary.md`" not in readme


def test_clean_all_help_and_readme_describe_per_run_atomicity(root: Path) -> None:
    repo = make_repo(root)
    config_home = root / "config-home"
    clean_help = stargate(repo, "clean", "--help", config_home=config_home)

    assert clean_help.returncode == 0, clean_help.stderr
    assert (
        "Clean every recorded run that passes the safety checks, skipping the rest."
        in compact(clean_help.stdout)
    )
    readme = compact((ROOT / "README.md").read_text())
    assert "removes each run that passes" in readme
    assert "are skipped, each reported with its reason, and the command exits `1`" in readme
    assert "Atomicity is per run" in readme
    assert "removes only that stale Git registration" in readme


def test_fanout_help_describes_mode_specific_flags(root: Path) -> None:
    repo = make_repo(root)
    config_home = root / "config-home"
    run_help = stargate(repo, "run", "--help", config_home=config_home)
    resume_help = stargate(repo, "resume", "--help", config_home=config_home)

    assert run_help.returncode == 0, run_help.stderr
    assert resume_help.returncode == 0, resume_help.stderr
    run_text = compact(run_help.stdout)
    resume_text = compact(resume_help.stdout)
    assert "Requires settings.commit: true" in run_text
    assert "--fan-out; that error is reported before a run is created" in run_text
    assert "Requires --fan-out; linear runs reject this option" in run_text
    assert "Fan-out mode is restored automatically; resume has no --fan-out switch" in resume_text
    assert "Linear runs only; fan-out resumes reject this option" in resume_text
    assert "Fan-out resumes reject this option" in resume_text
    assert "linear resumes reject this option" in resume_text


def test_mode_specific_flags_are_rejected_before_orchestration(root: Path) -> None:
    repo = make_repo(root)
    config_home = root / "config-home"

    no_commit = stargate(
        repo,
        "run",
        "--fan-out",
        "--no-commit",
        "demo task",
        config_home=config_home,
    )
    assert no_commit.returncode == 2, no_commit.stdout + no_commit.stderr
    assert "--no-commit cannot be combined with --fan-out" in no_commit.stderr
    assert not (repo / ".stargate").exists()

    linear_parallel = stargate(
        repo,
        "run",
        "--max-parallel-tasks",
        "2",
        "demo task",
        config_home=config_home,
    )
    assert linear_parallel.returncode == 2, linear_parallel.stdout + linear_parallel.stderr
    assert "--max-parallel-tasks requires --fan-out" in linear_parallel.stderr
    assert not (repo / ".stargate").exists()

    invalid_parallel = stargate(
        repo,
        "run",
        "--fan-out",
        "--max-parallel-tasks",
        "0",
        "demo task",
        config_home=config_home,
    )
    assert invalid_parallel.returncode == 2, invalid_parallel.stdout + invalid_parallel.stderr
    assert "must be a positive integer" in invalid_parallel.stderr
    assert not (repo / ".stargate").exists()

    states = repo / ".stargate" / "runs"
    fanout_state = states / "fanout" / "state.json"
    fanout_state.parent.mkdir(parents=True)
    fanout_state.write_text(json.dumps({"mode": "fanout"}))
    redo = stargate(
        repo,
        "resume",
        "fanout",
        "--redo",
        "developer",
        config_home=config_home,
    )
    assert redo.returncode == 2, redo.stdout + redo.stderr
    assert "--redo is not supported for fan-out runs" in redo.stderr

    linear_state = states / "linear" / "state.json"
    linear_state.parent.mkdir(parents=True)
    linear_state.write_text(json.dumps({"mode": "linear"}))
    resumed_parallel = stargate(
        repo,
        "resume",
        "linear",
        "--max-parallel-tasks",
        "2",
        config_home=config_home,
    )
    assert resumed_parallel.returncode == 2, resumed_parallel.stdout + resumed_parallel.stderr
    assert "--max-parallel-tasks is only supported for fan-out runs" in resumed_parallel.stderr


def test_disabled_commits_stop_fanout_before_run_creation(root: Path) -> None:
    repo = make_repo(root)
    config = root / "agents.yaml"
    write_config(
        config,
        'echo "VERDICT: APPROVED"',
        test_command="true",
        commit=False,
    )

    proc = run(repo, config, "demo task", "--fan-out")

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "Fan-out requires settings.commit: true; no run was created" in proc.stderr
    assert not (repo / ".stargate").exists()


def test_doctor_reports_fanout_settings_and_prompt(root: Path) -> None:
    repo = make_repo(root)
    config = root / "agents.yaml"
    write_config(config, 'echo "VERDICT: APPROVED"', test_command="true")
    data = yaml.safe_load(config.read_text())
    data["settings"].update(max_fanout_tasks=7, max_parallel_tasks=3)
    config.write_text(yaml.safe_dump(data))

    proc = doctor(repo, config)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert re.search(r"(?m)^  max_fanout_tasks\s+7\s+\[1\]$", proc.stdout)
    assert re.search(r"(?m)^  max_parallel_tasks\s+3\s+\[1\]$", proc.stdout)
    fanout_prompt = (ROOT / "stargate" / "prompts" / "fanout.md").resolve()
    assert re.search(rf"(?m)^  fanout\s+{re.escape(str(fanout_prompt))}$", proc.stdout)
    assert yaml.safe_load((ROOT / "stargate" / "agents.yaml").read_text())["version"] == 5
