"""Structured review output: JSON findings alongside the explicit verdict.

The verdict stays the reviewer's decision in every test here. What is new is
that the findings become report data -- persisted in state.json, tabulated in
summary.md -- while the fixer keeps receiving the reviewer's own response.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from tests.harness import ROOT, agent, make_repo, run

README = (ROOT / "README.md").read_text()
REVIEWER_PROMPT = (ROOT / "stargate" / "prompts" / "reviewer.md").read_text()

HIGH = {
    "severity": "high",
    "file": "app/storage.py",
    "line": 42,
    "finding": "Saving truncates the existing file before validating.",
    "why": "Invalid input destroys previously saved user data.",
}
LOW = {"severity": "low", "file": "app.py", "line": 1, "finding": "Nit.", "why": "Cosmetic."}


def _counting(marker: Path, script: str) -> list[str]:
    # `$call` is how many times this agent has run across the whole run,
    # resumes included: the only way to prove a resume did not buy a second
    # opinion on a tree a reviewer already judged.
    return agent(
        f'echo x >> {marker}; call=$(wc -l < {marker} | tr -d " "); {script}'
    )


def _calls(marker: Path) -> int:
    return len(marker.read_text().splitlines()) if marker.exists() else 0


def _review_file(root: Path, name: str, verdict: str, findings: list[dict]) -> Path:
    path = root / name
    path.write_text(json.dumps({"verdict": verdict, "findings": findings}, indent=2))
    return path


def _config(
    path: Path,
    reviewer: list[str],
    *,
    fixer: str = "echo change >> impl.txt; echo done",
    loops: int = 0,
) -> Path:
    path.write_text(
        yaml.safe_dump(
            {
                "agents": {
                    "noop": {"command": agent("echo done")},
                    "dev": {"command": agent("echo change >> impl.txt; echo done")},
                    "rev": {"command": reviewer},
                    "fix": {"command": agent(fixer)},
                },
                "workflow": {
                    "architect": "noop",
                    "developer": "dev",
                    "reviewer": "rev",
                    "fixer": "fix",
                },
                "settings": {
                    "max_review_loops": loops,
                    "test_command": "true",
                    "agent_timeout_seconds": 60,
                },
            }
        )
    )
    return path


def _second_repo(root: Path, name: str) -> Path:
    """A separate repository, so two runs cannot share one run directory."""
    (root / name).mkdir()
    return make_repo(root / name)


def _artifacts(repo: Path) -> Path:
    return next((repo / ".stargate" / "runs").iterdir())


def _state(repo: Path) -> dict:
    return json.loads((_artifacts(repo) / "state.json").read_text())


def _summary(repo: Path) -> str:
    return (_artifacts(repo) / "summary.md").read_text()


def _resume(repo: Path, config: Path, run_id: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "stargate", "--config", str(config), "resume", run_id],
        cwd=repo,
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )


def _run_id(repo: Path) -> str:
    return _artifacts(repo).name


def _blocking_hook(repo: Path) -> Path:
    # Stands in for the signing prompt that timed out: the verdict is reached
    # and the tree is final; only `git commit` refuses.
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    return hook


def test_json_findings_reach_the_summary_and_the_final_state(root: Path) -> None:
    """A finding the reviewer did not block on used to vanish with the prose.

    It is the whole point of the structured contract: an APPROVED review can
    still have told us something, and that has to survive to the report.
    """
    repo = make_repo(root)
    review = _review_file(root, "review.json", "APPROVED", [LOW])
    config = _config(root / "cfg.yaml", agent(f"cat {review}"))

    proc = run(repo, config, "small change")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    summary = _summary(repo)
    assert "## findings" in summary, summary
    assert "| low | app.py:1 | Nit. (why: Cosmetic.) |" in summary, summary
    state = _state(repo)
    assert state["findings"] == [
        {
            "severity": "low",
            "finding": "Nit.",
            "file": "app.py",
            "why": "Cosmetic.",
            "line": 1,
        }
    ], state["findings"]


def test_the_fixer_receives_the_original_json_review(root: Path) -> None:
    """No renderer in this version: the fixer reads the reviewer's own JSON.

    Inventing a rendered form would also invent blocking labels, which is the
    severity policy this version deliberately leaves out.
    """
    repo = make_repo(root)
    review = _review_file(root, "review.json", "CHANGES_REQUESTED", [HIGH])
    prompt = root / "fixer-prompt.txt"
    config = _config(
        root / "cfg.yaml",
        agent(f"cat {review}"),
        fixer=f'printf "%s" "$0" > {prompt}; echo change >> impl.txt; echo done',
        loops=1,
    )

    proc = run(repo, config, "needs a fix")

    # The reviewer never approves, so the loop ends at the last allowed pass.
    assert proc.returncode == 2, proc.stdout + proc.stderr
    seen = prompt.read_text()
    assert '"verdict": "CHANGES_REQUESTED"' in seen, seen
    assert '"severity": "high"' in seen, seen
    assert HIGH["finding"] in seen, seen
    # A rendered replacement or an invented blocking label would show up here.
    assert "[high]" not in seen, seen
    assert "non-blocking" not in seen, seen


def test_a_prose_reviewer_still_reaches_both_verdicts(root: Path) -> None:
    """The prose contract is what keeps runs created before this change alive.

    Their prompts are frozen at run start, so `resume` asks a reviewer that
    knows nothing about JSON -- and their state.json has no findings key.
    """
    repo = make_repo(root)
    reviews = root / "reviews.txt"
    config = _config(
        root / "prose.yaml", _counting(reviews, 'echo "VERDICT: APPROVED"')
    )
    hook = _blocking_hook(repo)

    blocked = run(repo, config, "prose reviewer")

    assert blocked.returncode == 5, blocked.stdout + blocked.stderr
    state = _state(repo)
    assert state["review"]["verdict"] == "APPROVED", state["review"]
    assert state["findings"] is None, state["findings"]

    hook.unlink()
    resumed = _resume(repo, config, _run_id(repo))

    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert _calls(reviews) == 1, "a prose checkpoint without findings must still be reused"

    rejecting = _config(
        root / "reject.yaml", agent('echo "VERDICT: CHANGES_REQUESTED"')
    )
    unapproved = run(repo, rejecting, "prose rejection")

    assert unapproved.returncode == 2, unapproved.stdout + unapproved.stderr


def test_malformed_review_output_names_both_contracts(root: Path) -> None:
    """A reviewer that satisfies neither contract must say so, and be resumable.

    The run has already paid for the review; aborting without naming what was
    wrong would make the operator re-run it to find out.
    """
    repo = make_repo(root)

    broken = _config(root / "broken.yaml", agent('echo "{not json at all"'))
    neither = run(repo, broken, "broken json")
    assert neither.returncode == 1, neither.stdout + neither.stderr
    assert "neither a JSON review object" in neither.stderr, neither.stderr
    assert "VERDICT: APPROVED" in neither.stderr, neither.stderr
    assert "Resume with:" in neither.stderr, neither.stderr

    verdictless = root / "verdictless.json"
    verdictless.write_text(json.dumps({"findings": [LOW]}))
    config = _config(root / "verdictless.yaml", agent(f"cat {verdictless}"))
    missing = run(repo, config, "no verdict")
    assert missing.returncode == 1, missing.stdout + missing.stderr
    assert "needs a 'verdict'" in missing.stderr, missing.stderr

    mislabelled = root / "severity.json"
    mislabelled.write_text(
        json.dumps(
            {"verdict": "APPROVED", "findings": [{"severity": "nit", "finding": "x"}]}
        )
    )
    config = _config(root / "severity.yaml", agent(f"cat {mislabelled}"))
    bad_severity = run(repo, config, "bad severity")
    assert bad_severity.returncode == 1, bad_severity.stdout + bad_severity.stderr
    assert "findings[0].severity must be one of" in bad_severity.stderr, (
        bad_severity.stderr
    )

    # bool is a subclass of int, so a naive isinstance check would take `true`
    # as a line number and carry it into the report.
    boolean_line = root / "line.json"
    boolean_line.write_text(
        json.dumps(
            {
                "verdict": "APPROVED",
                "findings": [{"severity": "low", "finding": "x", "line": True}],
            }
        )
    )
    config = _config(root / "line.yaml", agent(f"cat {boolean_line}"))
    bad_line = run(repo, config, "boolean line")
    assert bad_line.returncode == 1, bad_line.stdout + bad_line.stderr
    assert "findings[0].line must be an integer" in bad_line.stderr, bad_line.stderr


def test_findings_survive_a_resume_without_a_second_review(root: Path) -> None:
    """The fingerprint guarantee has to cover the structured payload too.

    Second half is the reason the findings are persisted at all: an approved
    resume skips the review loop entirely, so nothing reparses the artifact and
    only restored state can still fill the summary's table.
    """
    repo = make_repo(root)
    reviews = root / "reviews.txt"
    review = _review_file(root, "review.json", "CHANGES_REQUESTED", [HIGH])
    attempted = root / "fixer-attempted.txt"
    config = _config(
        root / "fixer.yaml",
        _counting(reviews, f"cat {review}"),
        # Fails the first time, so the run stops with the review recorded and
        # the tree exactly as that reviewer saw it. The marker lives outside the
        # worktree on purpose: touching a file in there would move the
        # fingerprint and legitimately cost a fresh review.
        fixer=f'[ -f {attempted} ] && {{ echo change >> impl.txt; echo done; }} '
        f"|| {{ touch {attempted}; exit 1; }}",
        loops=1,
    )

    interrupted = run(repo, config, "fixer fails once")

    assert interrupted.returncode == 1, interrupted.stdout + interrupted.stderr
    assert _calls(reviews) == 1, _calls(reviews)
    assert _state(repo)["findings"] == [HIGH], _state(repo)["findings"]

    resumed = _resume(repo, config, _run_id(repo))

    # This reviewer never approves, so the last allowed pass still requests
    # changes -- and the run commits that verdict anyway.
    assert resumed.returncode == 2, resumed.stdout + resumed.stderr
    # Review 1 was reused from its artifact; only the review the fixer's change
    # earned is new.
    assert _calls(reviews) == 2, _calls(reviews)

    approved = _second_repo(root, "second")
    approving = _review_file(root / "second", "review.json", "APPROVED", [LOW])
    approvals = root / "second" / "approvals.txt"
    config = _config(
        root / "second" / "cfg.yaml", _counting(approvals, f"cat {approving}")
    )
    hook = _blocking_hook(approved)

    blocked = run(approved, config, "commit fails after approval")

    assert blocked.returncode == 5, blocked.stdout + blocked.stderr
    hook.unlink()
    retried = _resume(approved, config, _run_id(approved))

    assert retried.returncode == 0, retried.stdout + retried.stderr
    assert _calls(approvals) == 1, "an approved resume must not pay for a second review"
    assert "| low | app.py:1 |" in _summary(approved), _summary(approved)


def test_a_legacy_run_resumes_from_its_frozen_prose_prompt(root: Path) -> None:
    """What a run created before this change actually looks like on resume.

    Its prompts were frozen at run start, so `resume` asks a reviewer that
    never heard of the JSON contract, and its state.json has no findings key at
    all -- not a null one. Both have to work, and the recorded prose review has
    to be reusable without paying for a second opinion.
    """
    repo = make_repo(root)
    reviews = root / "reviews.txt"
    frozen_prompt_seen = root / "frozen-prompt.txt"
    attempted = root / "fixer-attempted.txt"
    config = _config(
        root / "legacy.yaml",
        _counting(
            reviews,
            f'printf "%s" "$0" > {frozen_prompt_seen}; '
            '[ "$call" = 1 ] && echo "VERDICT: CHANGES_REQUESTED" '
            '|| echo "VERDICT: APPROVED"',
        ),
        fixer=f'[ -f {attempted} ] && {{ echo change >> impl.txt; echo done; }} '
        f"|| {{ touch {attempted}; exit 1; }}",
        loops=1,
    )

    interrupted = run(repo, config, "legacy run")

    assert interrupted.returncode == 1, interrupted.stdout + interrupted.stderr
    assert _calls(reviews) == 1, _calls(reviews)

    artifacts = _artifacts(repo)
    state_path = artifacts / "state.json"
    state = json.loads(state_path.read_text())
    # A run recorded before findings existed has no such key to read.
    del state["findings"]
    state_path.write_text(json.dumps(state, indent=2) + "\n")
    frozen = artifacts / "prompts" / "reviewer.md"
    frozen.write_text(
        "LEGACY PROMPT MARKER\n\nTASK: {task}\nBASE: {base_ref}\n"
        "PLAN: {plan}\nTESTS: {tests}\n\nEnd with exactly "
        "VERDICT: APPROVED or VERDICT: CHANGES_REQUESTED.\n"
    )

    resumed = _resume(repo, config, artifacts.name)

    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    # Review 1 came back from its prose artifact; review 2 is the one the
    # fixer's change earned.
    assert _calls(reviews) == 2, _calls(reviews)
    asked = frozen_prompt_seen.read_text()
    assert "LEGACY PROMPT MARKER" in asked, asked
    assert '"findings"' not in asked, "resume must use the frozen prompt, not the packaged one"
    assert "## findings" not in _summary(repo), _summary(repo)
    assert json.loads(state_path.read_text()).get("findings") is None


def test_a_finished_run_reports_findings_without_an_active_checkpoint(
    root: Path,
) -> None:
    """Two lifetimes: the checkpoint ends with the run, the report does not."""
    repo = make_repo(root)
    reviews = root / "reviews.txt"
    review = _review_file(root, "review.json", "APPROVED", [LOW])
    config = _config(root / "cfg.yaml", _counting(reviews, f"cat {review}"))

    proc = run(repo, config, "finished run")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    state = _state(repo)
    assert state["findings"], state
    assert state["review"] is None, state["review"]

    # With no checkpoint left, a further resume is owed a real review.
    resumed = _resume(repo, config, _run_id(repo))
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert _calls(reviews) == 2, _calls(reviews)

    uncommitted = _second_repo(root, "dirty")
    kept = _review_file(root / "dirty", "review.json", "APPROVED", [LOW])
    config = _config(root / "dirty" / "cfg.yaml", agent(f"cat {kept}"))

    proc = run(uncommitted, config, "no commit", "--no-commit")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _state(uncommitted)["findings"], _state(uncommitted)
    assert "## findings" in _summary(uncommitted), _summary(uncommitted)


def test_an_explicit_verdict_is_never_overridden(root: Path) -> None:
    """Pins this version's scope.

    Deriving the verdict from severities is a separate, unscheduled decision:
    no default for it is behaviour-neutral, in either direction. This test
    fails the moment such a policy leaks in early.
    """
    repo = make_repo(root)
    low_only = _review_file(root, "low.json", "CHANGES_REQUESTED", [LOW])
    config = _config(root / "low.yaml", agent(f"cat {low_only}"))

    blocked = run(repo, config, "blocks on a nit")

    assert blocked.returncode == 2, blocked.stdout + blocked.stderr
    assert "CHANGES_REQUESTED" in _summary(repo), _summary(repo)

    severe = _second_repo(root, "severe")
    approved_high = _review_file(root / "severe", "high.json", "APPROVED", [HIGH])
    config = _config(root / "severe" / "cfg.yaml", agent(f"cat {approved_high}"))

    lenient = run(severe, config, "approves with a high finding")

    assert lenient.returncode == 0, lenient.stdout + lenient.stderr
    assert "| high | app/storage.py:42 |" in _summary(severe), _summary(severe)


def test_per_pass_artifacts_keep_findings_replaced_in_the_final_report(
    root: Path,
) -> None:
    """The report describes the reviewed tree, not the run's history.

    Which is why a later evaluation of severity policy has to read the per-pass
    review artifacts: the final state intentionally forgets the first pass.
    """
    repo = make_repo(root)
    first = _review_file(root, "first.json", "CHANGES_REQUESTED", [LOW])
    second = _review_file(root, "second.json", "APPROVED", [])
    reviews = root / "reviews.txt"
    config = _config(
        root / "cfg.yaml",
        _counting(reviews, f'[ "$call" = 1 ] && cat {first} || cat {second}'),
        loops=1,
    )

    proc = run(repo, config, "fixed after one pass")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _calls(reviews) == 2, _calls(reviews)
    state = _state(repo)
    assert state["findings"] is None, state["findings"]
    assert "## findings" not in _summary(repo), _summary(repo)

    artifacts = _artifacts(repo)
    kept = (artifacts / "review-1.md").read_text()
    assert '"verdict": "CHANGES_REQUESTED"' in kept, kept
    assert '"severity": "low"' in kept, kept
    assert '"verdict": "APPROVED"' in (artifacts / "review-2.md").read_text()


def test_readme_and_reviewer_prompt_document_the_findings_contract(
    root: Path,
) -> None:
    """A contract the operator cannot look up is a contract they will break."""
    assert "## Structured review findings" in README
    for fragment in ('"verdict"', '"findings"', '"severity"', "high", "medium", "low"):
        assert fragment in README, fragment
    assert "VERDICT: APPROVED" in README, "the prose fallback must stay documented"

    for fragment in ("APPROVED", "CHANGES_REQUESTED", '"severity"', "demonstrated impact"):
        assert fragment in REVIEWER_PROMPT, fragment
