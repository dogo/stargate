"""The chain must not forget a finding that nobody blocked on in the last run."""

from __future__ import annotations

import json
import shlex
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

import yaml

from tests.harness import ROOT, agent, make_repo, run, write_config

MEDIUM = {
    "severity": "medium",
    "file": "app.py",
    "line": 12,
    "finding": "The export leaves {task} literal in the output.",
    "why": "Users receive an incomplete report.",
}
APPROVED = "printf '%s' " + shlex.quote(
    json.dumps({"verdict": "APPROVED", "findings": [MEDIUM]})
)


def _config(path: Path, reviewer: str = APPROVED, *, prompts_dir: str = "") -> Path:
    write_config(path, reviewer, test_command="true", prompts_dir=prompts_dir)
    config = yaml.safe_load(path.read_text())
    dump = shlex.quote(str(path.with_suffix(".prompt")))
    config["agents"]["noop"]["command"] = agent(
        f'printf "%s" "$0" > {dump}; echo "NAME: next step\n\nplan"'
    )
    path.write_text(yaml.safe_dump(config))
    return path


def _run_ids(repo: Path) -> list[str]:
    return sorted(path.name for path in (repo / ".stargate" / "runs").iterdir())


def _branch_of(repo: Path, run_id: str) -> str:
    state = repo / ".stargate" / "runs" / run_id / "state.json"
    return json.loads(state.read_text())["branch"]


def test_the_next_run_is_told_what_the_previous_review_left_unfixed(root: Path) -> None:
    repo = make_repo(root)
    config = _config(root / "cfg.yaml")
    first = run(repo, config, "first task")
    assert first.returncode == 0, first.stdout + first.stderr
    previous = _run_ids(repo)[0]
    branch = _branch_of(repo, previous)

    second = run(repo, config, "continue the work", "--base-ref", branch)

    assert second.returncode == 0, second.stdout + second.stderr
    prompt = config.with_suffix(".prompt").read_text()
    assert "## Known unresolved findings" in prompt, prompt
    assert f"last completed review of run {previous}" in prompt, prompt
    assert f"whose branch {branch}" in prompt, prompt
    assert "tree that review saw" in prompt, prompt
    assert "may already have resolved" in prompt, prompt
    assert f"- [medium] app.py:12 -- {MEDIUM['finding']}" in prompt, prompt
    assert f"(why: {MEDIUM['why']})" in prompt, prompt
    assert prompt.index("## Known unresolved findings") < prompt.index("USER TASK:"), prompt
    # The finding contains {task}; rescanning injected values would corrupt it.
    assert "USER TASK:\ncontinue the work" in prompt, prompt


def test_a_previous_run_without_findings_adds_no_empty_section(root: Path) -> None:
    repo = make_repo(root)
    config = _config(root / "cfg.yaml", 'echo \'{"verdict":"APPROVED","findings":[]}\'')
    first = run(repo, config, "first task")
    assert first.returncode == 0, first.stdout + first.stderr
    previous = _run_ids(repo)[0]
    state = json.loads((repo / ".stargate" / "runs" / previous / "state.json").read_text())
    assert state["findings"] is None, state
    branch = _branch_of(repo, previous)

    second = run(repo, config, "continue the work", "--base-ref", branch)

    assert second.returncode == 0, second.stdout + second.stderr
    prompt = config.with_suffix(".prompt").read_text()
    assert "Known unresolved findings" not in prompt, prompt
    assert "\n\n\n" not in prompt, prompt
    template = (ROOT / "stargate" / "prompts" / "architect.md").read_text()
    expected = template.replace("{known_findings}", "").replace(
        "{task}", "continue the work"
    ).replace("{base_ref}", branch)
    assert prompt == expected, prompt


def test_a_base_ref_outside_the_chain_inherits_nothing(root: Path) -> None:
    repo = make_repo(root)
    config = _config(root / "cfg.yaml")
    first = run(repo, config, "first task")
    assert first.returncode == 0, first.stdout + first.stderr

    second = run(repo, config, "unrelated work", "--base-ref", "main")

    assert second.returncode == 0, second.stdout + second.stderr
    prompt = config.with_suffix(".prompt").read_text()
    assert "Known unresolved findings" not in prompt, prompt
    assert MEDIUM["finding"] not in prompt, prompt


def test_an_architect_prompt_without_the_placeholder_still_runs(root: Path) -> None:
    repo = make_repo(root)
    prompts = root / "prompts"
    prompts.mkdir()
    (prompts / "architect.md").write_text("PLAN {task} for {base_ref}.\n")
    config = _config(root / "cfg.yaml", prompts_dir=str(prompts))
    first = run(repo, config, "first task")
    assert first.returncode == 0, first.stdout + first.stderr
    branch = _branch_of(repo, _run_ids(repo)[0])

    second = run(repo, config, "continue the work", "--base-ref", branch)

    assert second.returncode == 0, second.stdout + second.stderr
    prompt = config.with_suffix(".prompt").read_text()
    assert prompt == f"PLAN continue the work for {branch}.\n", prompt
    assert "Known unresolved findings" not in prompt, prompt
    assert MEDIUM["finding"] not in prompt, prompt


def test_an_unreadable_or_findingless_previous_run_is_not_an_error(root: Path) -> None:
    from stargate.run import inherited_findings

    output = StringIO()
    with redirect_stdout(output), redirect_stderr(output):
        assert inherited_findings(root, "stargate/previous") == ("", [])
        runs = root / ".stargate" / "runs"
        runs.mkdir(parents=True)
        for name, contents in (
            ("garbage", b"not json"),
            ("encoding", b"\xff"),
            ("non-object", b"[]"),
            ("unrelated", json.dumps({"branch": "stargate/other", "findings": [MEDIUM]}).encode()),
        ):
            path = runs / name
            path.mkdir()
            (path / "state.json").write_bytes(contents)
        # Reading a directory as state.json raises OSError even when run as root.
        (runs / "unreadable" / "state.json").mkdir(parents=True)
        assert inherited_findings(root, "stargate/previous") == ("", [])
        matching = runs / "matching"
        matching.mkdir()
        state = {"branch": "stargate/previous"}
        (matching / "state.json").write_text(json.dumps(state))
        assert inherited_findings(root, "stargate/previous") == ("", [])
        for findings in (None, [], {}, "invalid"):
            state["findings"] = findings
            (matching / "state.json").write_text(json.dumps(state))
            assert inherited_findings(root, "stargate/previous") == ("", [])
        assert inherited_findings(root, "main") == ("", [])
    assert output.getvalue() == "", output.getvalue()


def test_the_newest_exact_branch_match_never_revives_older_findings(root: Path) -> None:
    from stargate.run import inherited_findings

    # Names deliberately cannot be reconstructed from the branch, including its suffix.
    branch = "stargate/renamed-by-architect-20260911-120000-2"
    runs = root / ".stargate" / "runs"
    older = runs / "20260910-original-task"
    newer = runs / "20260911-different-task"
    older.mkdir(parents=True)
    newer.mkdir()
    (older / "state.json").write_text(json.dumps({"branch": branch, "findings": [MEDIUM]}))
    state = {"branch": branch, "findings": None}
    (newer / "state.json").write_text(json.dumps(state))
    assert inherited_findings(root, branch) == ("", [])

    state["findings"] = [MEDIUM]
    (newer / "state.json").write_text(json.dumps(state))
    assert inherited_findings(root, branch) == (newer.name, [MEDIUM])
    state["run_id"] = "recorded-source-id"
    (newer / "state.json").write_text(json.dumps(state))
    assert inherited_findings(root, branch) == ("recorded-source-id", [MEDIUM])
    assert inherited_findings(root, "refs/heads/" + branch) == ("", [])
    assert inherited_findings(root, branch.removesuffix("-2")) == ("", [])


def test_only_the_last_review_is_inherited_even_when_the_tree_moved_on(root: Path) -> None:
    from stargate.core import RunContext
    from stargate.stages import _known_findings_section

    artifacts = root / ".stargate" / "runs" / "previous"
    artifacts.mkdir(parents=True)
    (artifacts / "review-1.md").write_text("earlier finding that was resolved")
    (artifacts / "state.json").write_text(json.dumps({
        "branch": "stargate/previous",
        "status": "budget_exceeded",
        "review": {"attempt": 2, "fixed": True},
        "findings": [
            "hand-edited\nentry",
            {"severity": "low", "finding": "first low"},
            MEDIUM,
            {"severity": "low", "finding": "second low"},
            {"severity": ["invalid"], "finding": "unknown severity"},
            {"severity": "high", "finding": "a line with no file", "line": 7},
        ],
    }))
    ctx = RunContext(
        repo=root, config={}, run_id="next", slug="next", branch="stargate/next",
        base_ref="stargate/previous", base_commit="", worktree=root, artifacts=root,
    )

    section = _known_findings_section(ctx)

    assert "earlier finding that was resolved" not in section, section
    assert MEDIUM["finding"] in section, section
    assert "may already have resolved" in section, section
    assert section.index("[medium]") < section.index("first low"), section
    assert section.index("first low") < section.index("second low"), section
    assert "- [?] hand-edited entry" in section, section
    assert "unknown severity" in section, section
    # file and line are independently optional; the line must survive alone.
    assert "- [high] -:7 -- a line with no file" in section, section
