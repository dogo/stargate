"""Approval changes only when an operator opts into a severity policy."""

from pathlib import Path

import yaml

from tests.harness import (
    ROOT,
    agent,
    doctor,
    git_output,
    make_repo,
    run,
    stargate,
    write_fanout_config,
)
from tests.test_fanout_finish import _single_task_graph
from tests.test_review_findings import (
    HIGH,
    LOW,
    _artifacts,
    _blocking_hook,
    _calls,
    _config,
    _counting,
    _resume,
    _review_file,
    _run_id,
    _second_repo,
    _state,
    _summary,
)


def _policy(config: Path, value: object) -> Path:
    data = yaml.safe_load(config.read_text())
    data["settings"]["blocking_severities"] = value
    config.write_text(yaml.safe_dump(data))
    return config


def test_an_absent_or_empty_policy_leaves_the_reviewer_deciding(root: Path) -> None:
    for index, policy in enumerate(("absent", [], None)):
        for verdict, finding, expected in (("CHANGES_REQUESTED", LOW, 2), ("APPROVED", HIGH, 0)):
            repo = _second_repo(root, f"case-{index}-{verdict}")
            review = _review_file(repo.parent, "review.json", verdict, [finding])
            prompt = repo.parent / "prompt.txt"
            config = _config(
                repo.parent / "cfg.yaml", agent(f"cat {review}"), loops=1,
                fixer=f'printf "%s" "$0" > {prompt}; echo done',
            )
            if policy != "absent":
                _policy(config, policy)

            proc = run(repo, config, "explicit verdict is authoritative")

            assert proc.returncode == expected, proc.stdout + proc.stderr
            assert f"Verdict: {verdict}" in _summary(repo), _summary(repo)
            assert "blocking severities" not in proc.stdout, proc.stdout
            if prompt.exists():
                assert "ORCHESTRATOR NOTE" not in prompt.read_text(), prompt.read_text()


def test_a_lenient_policy_approves_a_review_that_only_found_nits(root: Path) -> None:
    repo = make_repo(root)
    review = _review_file(root, "review.json", "CHANGES_REQUESTED", [LOW])
    config = _policy(_config(root / "cfg.yaml", agent(f"cat {review}"), loops=1),
                     ["high", "medium"])

    proc = run(repo, config, "record nits without a fixer cycle")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "FIXER" not in proc.stdout, proc.stdout
    assert "blocking_severities" in proc.stdout, proc.stdout
    assert "Verdict: APPROVED" in _summary(repo), _summary(repo)
    assert "| low | app.py:1 |" in _summary(repo), _summary(repo)
    assert _state(repo)["findings"] == [LOW], _state(repo)
    assert "APPROVED" in git_output(Path(_state(repo)["worktree"]), "log", "-1", "--format=%B")


def test_a_strict_policy_blocks_an_approval_and_tells_the_fixer_why(root: Path) -> None:
    repo = make_repo(root)
    review = _review_file(root, "review.json", "APPROVED", [
        {**HIGH, "finding": "Keep {tests}, {plan}, and {review} literal."}, LOW,
    ])
    prompt = root / "fixer-prompt.txt"
    config = _policy(_config(
        root / "cfg.yaml", agent(f"cat {review}"), loops=1,
        fixer=f'printf "%s" "$0" > {prompt}; echo done',
    ), [" HIGH ", "high", "medium"])

    proc = run(repo, config, "block demonstrated harm despite approval")

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "=== FIXER 1 ===" in proc.stdout, proc.stdout
    seen = prompt.read_text()
    assert review.read_text() in seen, seen
    assert seen.index("ORCHESTRATOR NOTE") > seen.index(review.read_text()), seen
    assert "settings.blocking_severities is active: high, medium." in seen, seen
    assert "other findings are recorded but do not block" in seen, seen
    assert "Verdict: CHANGES_REQUESTED" in _summary(repo), _summary(repo)


def test_a_review_with_no_findings_keeps_its_explicit_verdict(root: Path) -> None:
    for verdict, expected in (("CHANGES_REQUESTED", 2), ("APPROVED", 0)):
        repo = _second_repo(root, verdict)
        review = _review_file(repo.parent, "review.json", verdict, [])
        prompt = repo.parent / "prompt.txt"
        config = _policy(_config(
            repo.parent / "cfg.yaml", agent(f"cat {review}"), loops=1,
            fixer=f'printf "%s" "$0" > {prompt}; echo done',
        ), ["high", "medium", "low"])

        proc = run(repo, config, "no evidence for deriving a verdict")

        assert proc.returncode == expected, proc.stdout + proc.stderr
        assert "derived" not in proc.stdout, proc.stdout
        if prompt.exists():
            assert "keep the reviewer's explicit verdict" in prompt.read_text(), prompt.read_text()


def test_prose_keeps_its_literal_verdict_under_an_active_policy(root: Path) -> None:
    for verdict, expected in (("CHANGES_REQUESTED", 2), ("APPROVED", 0)):
        repo = _second_repo(root, verdict)
        config = _policy(_config(repo.parent / "cfg.yaml", agent(f'echo "VERDICT: {verdict}"')),
                         ["high", "medium", "low"])

        proc = run(repo, config, "frozen prose prompts still decide")

        assert proc.returncode == expected, proc.stdout + proc.stderr
        assert "derived" not in proc.stdout, proc.stdout


def test_an_invalid_policy_fails_before_any_agent_runs(root: Path) -> None:
    for index, value in enumerate(("high", ["critical"], 5, "", True, [False], [{}])):
        for mode in ((), ("--fan-out",)):
            repo = _second_repo(root, f"case-{index}-{len(mode)}")
            marker = repo.parent / "called.txt"
            config = _policy(_config(repo.parent / "cfg.yaml", agent("echo done")), value)
            data = yaml.safe_load(config.read_text())
            data["agents"]["noop"]["command"] = agent(f"touch {marker}; echo plan")
            config.write_text(yaml.safe_dump(data))

            proc = run(repo, config, "reject invalid policy before spending", *mode)

            assert proc.returncode == 1, proc.stdout + proc.stderr
            for fragment in ("settings.blocking_severities", "high, medium, low"):
                assert fragment in proc.stderr, proc.stderr
            assert repr(value[0] if isinstance(value, list) else value) in proc.stderr, proc.stderr
            assert not marker.exists(), proc.stdout
            assert not (repo / ".stargate" / "runs").exists(), proc.stdout


def test_an_invalid_resume_policy_leaves_the_checkpoint_and_agents_untouched(root: Path) -> None:
    repo = make_repo(root)
    reviews = root / "calls.txt"
    config = _config(root / "cfg.yaml", _counting(reviews, 'echo "VERDICT: APPROVED"'))
    _blocking_hook(repo)
    first = run(repo, config, "invalid override must be free")
    assert first.returncode == 5, first.stdout + first.stderr
    before = (_artifacts(repo) / "state.json").read_bytes()

    resumed = _resume(repo, _policy(config, ["critical"]), _run_id(repo))

    assert resumed.returncode == 1, resumed.stdout + resumed.stderr
    assert "'critical'" in resumed.stderr, resumed.stderr
    assert _calls(reviews) == 1, _calls(reviews)
    assert (_artifacts(repo) / "state.json").read_bytes() == before


def test_a_resume_can_tighten_the_policy_over_a_recorded_approval(root: Path) -> None:
    repo = make_repo(root)
    reviews = root / "calls.txt"
    review = _review_file(root, "review.json", "APPROVED", [HIGH])
    config = _config(root / "cfg.yaml", _counting(reviews, f"cat {review}"), loops=1)
    hook = _blocking_hook(repo)
    first = run(repo, config, "reuse an approved review under a stricter policy")
    assert first.returncode == 5, first.stdout + first.stderr
    assert _state(repo)["review"]["verdict"] == "APPROVED", _state(repo)
    hook.unlink()

    resumed = _resume(repo, _policy(config, ["high"]), _run_id(repo))

    assert resumed.returncode == 2, resumed.stdout + resumed.stderr
    assert "REVIEW 1 (skipped, reusing" in resumed.stdout, resumed.stdout
    assert "=== FIXER 1 ===" in resumed.stdout, resumed.stdout
    assert _calls(reviews) == 2, "only the post-fix tree needs another review"


def test_a_resume_can_loosen_the_policy_over_recorded_changes(root: Path) -> None:
    repo = make_repo(root)
    reviews = root / "calls.txt"
    review = _review_file(root, "review.json", "CHANGES_REQUESTED", [LOW])
    config = _config(root / "cfg.yaml", _counting(reviews, f"cat {review}"),
                     fixer="exit 1", loops=1)
    first = run(repo, config, "release nits without another review or fix")
    assert first.returncode == 1, first.stdout + first.stderr

    resumed = _resume(repo, _policy(config, ["high", "medium"]), _run_id(repo))

    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert _calls(reviews) == 1, _calls(reviews)
    assert "FIXER" not in resumed.stdout, resumed.stdout
    assert "Verdict: APPROVED" in _summary(repo), _summary(repo)
    assert "| low | app.py:1 |" in _summary(repo), _summary(repo)


def test_a_reduced_loop_budget_stops_replay_with_or_without_a_policy(
    root: Path,
) -> None:
    for index, policy in enumerate((None, ["high"])):
        repo = _second_repo(root, f"case-{index}")
        reviews = repo.parent / "calls.txt"
        review = _review_file(repo.parent, "review.json", "CHANGES_REQUESTED", [LOW])
        attempted = repo.parent / "fixer.txt"
        config = _config(
            repo.parent / "cfg.yaml", _counting(reviews, f"cat {review}"), loops=2,
            fixer=f'[ -f {attempted} ] && exit 1; touch {attempted}; echo change >> impl.txt',
        )
        first = run(repo, config, "replaying a review still obeys the loop budget")
        assert first.returncode == 1, first.stdout + first.stderr
        assert _state(repo)["review"]["attempt"] == 2, _state(repo)
        _config(config, _counting(reviews, f"cat {review}"), loops=0)
        if policy is not None:
            _policy(config, policy)

        resumed = _resume(repo, config, _run_id(repo))

        assert resumed.returncode == 2, resumed.stdout + resumed.stderr
        assert "REVIEW 2" not in resumed.stdout, resumed.stdout
        assert "FIXER" not in resumed.stdout, resumed.stdout
        assert _calls(reviews) == 2, _calls(reviews)
        assert "Verdict: CHANGES_REQUESTED" in _summary(repo), _summary(repo)


def test_fanout_shares_the_policy_and_rejects_bad_resume_settings_before_agents(root: Path) -> None:
    repo = make_repo(root)
    graph = root / "tasks.json"
    _single_task_graph(graph)
    review = _review_file(root, "review.json", "APPROVED", [HIGH])
    calls = root / "calls.txt"
    config = root / "cfg.yaml"
    write_fanout_config(config, graph, "echo unit > unit.txt",
                        reviewer=f"echo called >> {calls}; cat {review}")
    _policy(config, ["high"])
    # Fail only the terminal integration commit; task commits must reach review.
    hook = repo / ".git" / "hooks" / "commit-msg"
    hook.write_text('#!/bin/sh\n! grep -q "CHANGES_REQUESTED" "$1"\n')
    hook.chmod(0o755)
    first = run(repo, config, "policy on the combined tree", "--fan-out")
    assert first.returncode == 5, first.stdout + first.stderr
    assert _state(repo)["review"]["verdict"] == "CHANGES_REQUESTED", _state(repo)
    before = (_artifacts(repo) / "state.json").read_bytes()

    invalid = _resume(repo, _policy(config, ["critical"]), _run_id(repo))
    assert invalid.returncode == 1, invalid.stdout + invalid.stderr
    assert "'critical'" in invalid.stderr, invalid.stderr
    assert (_artifacts(repo) / "state.json").read_bytes() == before
    assert _calls(calls) == 1, _calls(calls)
    hook.unlink()

    resumed = _resume(repo, _policy(config, ["medium"]), _run_id(repo))
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert "REVIEW 1 (skipped, reusing" in resumed.stdout, resumed.stdout
    assert _calls(calls) == 1, _calls(calls)


def test_disabling_a_policy_on_resume_restores_reviewer_authority_in_both_directions(
    root: Path,
) -> None:
    for explicit, finding, expected in (("APPROVED", HIGH, 0), ("CHANGES_REQUESTED", LOW, 2)):
        repo = _second_repo(root, explicit)
        reviews = repo.parent / "calls.txt"
        review = _review_file(repo.parent, "review.json", explicit, [finding])
        config = _policy(_config(repo.parent / "cfg.yaml", _counting(reviews, f"cat {review}")),
                         ["high"])
        hook = _blocking_hook(repo)
        first = run(repo, config, "removing a policy is also a policy change")
        assert first.returncode == 5, first.stdout + first.stderr
        assert _state(repo)["review"]["verdict"] != explicit, _state(repo)
        hook.unlink()

        resumed = _resume(repo, _policy(config, []), _run_id(repo))

        assert resumed.returncode == expected, resumed.stdout + resumed.stderr
        assert _calls(reviews) == 1, _calls(reviews)
        assert f"Verdict: {explicit}" in _summary(repo), _summary(repo)


def test_without_a_policy_an_edited_artifact_cannot_replace_the_recorded_decision(
    root: Path,
) -> None:
    for verdict, edited, expected, calls in (
        ("CHANGES_REQUESTED", "APPROVED", 2, 2),
        ("APPROVED", "CHANGES_REQUESTED", 0, 1),
    ):
        repo = _second_repo(root, verdict)
        reviews = repo.parent / "calls.txt"
        review = _review_file(repo.parent, "review.json", verdict, [LOW])
        config = _config(repo.parent / "cfg.yaml", _counting(reviews, f"cat {review}"))
        hook = _blocking_hook(repo)
        first = run(repo, config, "artifact edits must not change default resume semantics")
        assert first.returncode == 5, first.stdout + first.stderr
        assert set(_state(repo)["review"]) == {"attempt", "verdict", "fingerprint", "fixed"}
        _review_file(_artifacts(repo), "review-1.md", edited, [HIGH])
        hook.unlink()

        resumed = _resume(repo, config, _run_id(repo))

        assert resumed.returncode == expected, resumed.stdout + resumed.stderr
        assert _calls(reviews) == calls, resumed.stdout + resumed.stderr
        assert _state(repo)["findings"] == [LOW], _state(repo)
        assert f"Verdict: {verdict}" in _summary(repo), _summary(repo)


def test_a_policy_enabled_on_resume_can_be_disabled_without_rebuying_the_review(
    root: Path,
) -> None:
    for explicit, finding, expected in (("APPROVED", HIGH, 0), ("CHANGES_REQUESTED", LOW, 2)):
        repo = _second_repo(root, explicit)
        reviews = repo.parent / "calls.txt"
        review = _review_file(repo.parent, "review.json", explicit, [finding])
        config = _config(repo.parent / "cfg.yaml", _counting(reviews, f"cat {review}"))
        hook = _blocking_hook(repo)
        first = run(repo, config, "track the checkpoint policy beyond the initial config")
        assert first.returncode == 5, first.stdout + first.stderr

        enabled = _resume(repo, _policy(config, ["high"]), _run_id(repo))
        assert enabled.returncode == 5, enabled.stdout + enabled.stderr
        assert _state(repo)["review"]["verdict"] != explicit, _state(repo)
        hook.unlink()

        disabled = _resume(repo, _policy(config, []), _run_id(repo))

        assert disabled.returncode == expected, disabled.stdout + disabled.stderr
        assert _calls(reviews) == 1, disabled.stdout + disabled.stderr
        assert f"Verdict: {explicit}" in _summary(repo), _summary(repo)


def test_a_resume_uses_the_frozen_policy_unless_config_is_explicit(root: Path) -> None:
    repo = make_repo(root)
    reviews = root / "calls.txt"
    review = _review_file(root, "review.json", "APPROVED", [HIGH])
    config = _policy(_config(root / "cfg.yaml", _counting(reviews, f"cat {review}")), ["high"])
    hook = _blocking_hook(repo)
    first = run(repo, config, "keep the run's policy frozen")
    assert first.returncode == 5, first.stdout + first.stderr
    hook.unlink()
    _policy(config, [])

    resumed = stargate(repo, "resume", _run_id(repo), config_home=root / "config-home")

    assert resumed.returncode == 2, resumed.stdout + resumed.stderr
    assert _calls(reviews) == 1, _calls(reviews)
    assert "Verdict: CHANGES_REQUESTED" in _summary(repo), _summary(repo)


def test_an_active_policy_needs_an_artifact_before_reusing_an_approval(root: Path) -> None:
    repo = make_repo(root)
    reviews = root / "calls.txt"
    review = _review_file(root, "review.json", "APPROVED", [HIGH])
    config = _config(root / "cfg.yaml", _counting(reviews, f"cat {review}"))
    hook = _blocking_hook(repo)
    first = run(repo, config, "a stricter policy must not trust missing evidence")
    assert first.returncode == 5, first.stdout + first.stderr
    hook.unlink()
    (_artifacts(repo) / "review-1.md").unlink()

    resumed = _resume(repo, _policy(config, ["high"]), _run_id(repo))

    assert resumed.returncode == 2, resumed.stdout + resumed.stderr
    assert _calls(reviews) == 2, _calls(reviews)


def test_doctor_reports_the_policy_and_where_it_came_from(root: Path) -> None:
    repo = make_repo(root)
    config = _config(root / "cfg.yaml", agent('echo "VERDICT: APPROVED"'))
    for value in (None, ["high"]):
        if value:
            _policy(config, value)
        proc = doctor(repo, config)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        row = next(line for line in proc.stdout.splitlines() if "blocking_severities" in line)
        assert ("['high']" if value else "[]") in row, row
        assert ("[1]" if value else "(default)") in row, row


def test_doctor_rejects_a_policy_that_a_run_would_refuse(root: Path) -> None:
    """Finding out from `doctor` is the whole point of `doctor`.

    Reporting a value that `run` will abort on would make the setup checker the
    wrong place to learn the setup is broken.
    """
    repo = make_repo(root)
    config = _config(root / "cfg.yaml", agent('echo "VERDICT: APPROVED"'))
    for value, expected in (("high", "must be a list"), (["critical"], "unknown severity")):
        _policy(config, value)

        proc = doctor(repo, config)

        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "ERROR    settings.blocking_severities" in proc.stdout, proc.stdout
        assert expected in proc.stdout, proc.stdout
        # The offending value still appears, so the message and the input agree.
        row = next(
            line for line in proc.stdout.splitlines()
            if line.startswith("  blocking_severities")
        )
        assert repr(value) in row, row


def test_the_readme_documents_the_policy_and_its_lack_of_a_neutral_default(root: Path) -> None:
    readme = (ROOT / "README.md").read_text()
    for fragment in (
        "| `blocking_severities` | `[]` |", "### Severity policy (opt-in)",
        "no behavior-neutral derived default", "no findings", "Prose", "unchanged",
        "misclassified severity can end a run before the fix",
        # doctor rejects an invalid policy rather than reporting it as normal.
        "reported\nhere as an `ERROR` and exits `1`",
        # The cost of re-reading the artifact while a policy is active.
        "the artifact **outranks** the recorded verdict in both directions",
    ):
        assert fragment in readme, fragment
