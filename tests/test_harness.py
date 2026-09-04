"""Regression coverage for the importable shared test harness."""

import io
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from tests.harness import ROOT, git_output, make_repo


def test_shared_harness_is_discovered_and_reusable(root: Path) -> None:
    repo = make_repo(root)

    assert ROOT == Path(__file__).resolve().parents[1]
    assert git_output(repo, "branch", "--show-current") == "main"


def test_discovery_order_is_stable(root: Path) -> None:
    import test_stargate

    tests, failures = test_stargate._discover_tests()
    names = [name for name, _ in tests]

    assert not failures
    assert names == sorted(names)
    assert "test_discovery_order_is_stable" in names
    assert "test_shared_harness_is_discovered_and_reusable" in names


def test_runner_reports_named_failures(root: Path) -> None:
    import test_stargate

    def fail(_root: Path) -> None:
        raise AssertionError("intentional failure")

    def discover_failure():
        return [("test_intentional_failure", fail)], []

    original_discovery = test_stargate._discover_tests
    test_stargate._discover_tests = discover_failure
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = test_stargate._run_tests()
    finally:
        test_stargate._discover_tests = original_discovery

    assert exit_code == 1
    assert "not ok  test_intentional_failure" in stdout.getvalue()
    assert "--- test_intentional_failure ---" in stderr.getvalue()
    assert "1 failed, 0 passed" in stderr.getvalue()


def test_runner_does_not_count_an_import_failure_as_a_passing_test(root: Path) -> None:
    import test_stargate

    def passes(_root: Path) -> None:
        pass

    def discover_with_import_failure():
        return [("test_that_ran", passes)], [("tests.test_broken", "import failed")]

    original_discovery = test_stargate._discover_tests
    test_stargate._discover_tests = discover_with_import_failure
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = test_stargate._run_tests()
    finally:
        test_stargate._discover_tests = original_discovery

    assert exit_code == 1
    assert "ok  test_that_ran" in stdout.getvalue()
    assert "--- tests.test_broken ---" in stderr.getvalue()
    assert "1 failed, 1 passed" in stderr.getvalue()
