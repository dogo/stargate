"""Publishing happens only when a person asks in that invocation."""

import io
import json
import os
import shlex
import shutil
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import yaml

from stargate.config import pull_request_command
from stargate.core import StargateError
from stargate.publish import open_pull_request, publication_command, push_branch, select_remote
from stargate.run import load_run
from stargate.stages import _publish, finish
from tests.harness import (
    ROOT,
    agent,
    doctor,
    git_output,
    make_repo,
    run,
    stargate,
    write_config,
    write_fanout_config,
)


def _origin(root: Path, repo: Path) -> Path:
    bare = root / "origin.git"
    git_output(root, "init", "-q", "--bare", str(bare))
    git_output(repo, "remote", "add", "origin", str(bare))
    return bare


def _command(root: Path) -> dict:
    return {"command": [
        "/bin/sh", "-c",
        f'cat > {shlex.quote(str(root / "pr-body"))}; '
        f'printf "%s\\n%s\\n" "$1" "$2" >> {shlex.quote(str(root / "pr-args"))}; '
        'echo https://example.invalid/pr/1',
        "_", "{branch}", "{title}",
    ]}


def _state(repo: Path) -> tuple[Path, dict]:
    path = next((repo / ".stargate/runs").glob("*/state.json")).resolve()
    return path, json.loads(path.read_text())


def test_a_configured_publish_block_publishes_nothing_without_the_flag(root: Path) -> None:
    repo = make_repo(root)
    _origin(root, repo)
    config = root / "config.yaml"
    write_config(config, 'echo "VERDICT: APPROVED"', test_command="true",
                 pull_request=_command(root))
    proc = run(repo, config)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    path, state = _state(repo)
    assert not (root / "pr-args").exists(), proc.stdout + proc.stderr
    assert git_output(repo, "ls-remote", "--heads", "origin") == ""
    assert not (path.parent / "pull-request.md").exists()
    assert not ({"pr", "publish", "pull_request"} & state.keys()), state
    assert "Nothing was merged, pushed, or deleted automatically." in proc.stdout, proc.stdout


def test_an_approved_run_publishes_its_task_title_findings_tests_and_source(root: Path) -> None:
    repo = make_repo(root)
    bare = _origin(root, repo)
    config, source = root / "config.yaml", root / "task.txt"
    task = '\n \t\n  Keep {branch}  and "quotes" literal  \n\nDetailed task, not the title.\n'
    source.write_text(task)
    review = json.dumps({"verdict": "APPROVED", "findings": [
        {"severity": "low", "file": "app.py", "line": 1,
         "finding": "Small issue", "why": "clarity"},
    ]})
    write_config(config, f"echo {shlex.quote(review)}", test_command="true",
                 pull_request=_command(root), task_sources=[
                     {"hosts": ["tracker.example"], "commands": [["cat", str(source)]]},
                 ])
    url = "https://tracker.example/items/42"
    proc = run(repo, config, "", "--from", url, "--pr")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    path, state = _state(repo)
    assert git_output(bare, "rev-parse", state["branch"]) == state["commit"]
    assert (root / "pr-args").read_text().splitlines() == [
        state["branch"], 'Keep {branch} and "quotes" literal',
    ]
    body = (root / "pr-body").read_text()
    # Bulleted, not bare lines: plain newlines render as one paragraph.
    for expected in ("- **Verdict:** APPROVED",
                     "| low | app.py:1 | Small issue (why: clarity) |",
                     "- **Tests:** true (exit 0)", url, task):
        assert expected in body, body
    assert body == (path.parent / "pull-request.md").read_text()
    assert "https://example.invalid/pr/1" in proc.stdout, proc.stdout + proc.stderr
    assert "Nothing was merged, pushed" not in proc.stdout, proc.stdout
    assert git_output(repo, "branch", "--show-current") == "main"


def test_a_successful_silent_pr_command_confirms_publication(root: Path) -> None:
    for index, script in enumerate(("cat > /dev/null", "cat > /dev/null; printf ' \\n'")):
        case = root / str(index)
        case.mkdir()
        repo = make_repo(case)
        bare = _origin(case, repo)
        config = case / "config.yaml"
        write_config(config, 'echo "VERDICT: APPROVED"', test_command="true", pull_request={
            "command": ["/bin/sh", "-c", script, "_", "{branch}"],
        })
        proc = run(repo, config, "Task", "--pr")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        _, state = _state(repo)
        assert git_output(bare, "rev-parse", state["branch"]) == state["commit"]
        assert "Pull request: Opened successfully." in proc.stdout, proc.stdout + proc.stderr


def test_publishing_help_discloses_the_cost_and_verdict_change_of_resuming(root: Path) -> None:
    readme = " ".join((ROOT / "README.md").read_text().split())
    assert "re-runs the review at token cost and may replace the recorded verdict" in readme
    for command in ("run", "resume"):
        proc = stargate(root, command, "--help", config_home=root / "config-home")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        help_text = " ".join(proc.stdout.split())
        assert "Resuming a finished run re-runs review at token cost" in help_text, help_text
        assert "may replace the recorded verdict" in help_text, help_text


def test_unapproved_or_failing_tests_return_the_decision_with_manual_commands(root: Path) -> None:
    for verdict, tests, code in (("CHANGES_REQUESTED", "true", 2), ("APPROVED", "false", 3)):
        case = root / verdict
        case.mkdir()
        repo = make_repo(case)
        _origin(case, repo)
        config = case / "config.yaml"
        block = _command(case)
        write_config(config, f'echo "VERDICT: {verdict}"', test_command=tests, pull_request=block)
        proc = run(repo, config, "Task title", "--pr")
        assert proc.returncode == code, proc.stdout + proc.stderr
        path, state = _state(repo)
        assert git_output(repo, "ls-remote", "--heads", "origin") == ""
        assert not (case / "pr-args").exists(), proc.stdout + proc.stderr
        command = publication_command({"pull_request": block}, state["branch"], "Task title")
        body_path = shlex.quote(str(path.parent / "pull-request.md"))
        for expected in (state["worktree"], f"git push origin {state['branch']}",
                         f"{shlex.join(command)} < {body_path}"):
            assert expected in proc.stdout, proc.stdout + proc.stderr


def test_resume_requires_the_flag_again_and_uses_the_frozen_publish_config(root: Path) -> None:
    repo = make_repo(root)
    _origin(root, repo)
    config, ready = root / "config.yaml", root / "ready"
    write_config(config, 'echo "VERDICT: APPROVED"', test_command="true",
                 pull_request=_command(root))
    data = yaml.safe_load(config.read_text())
    data["agents"]["dev"]["command"] = agent(
        f"test -f {ready} || exit 1; echo change >> impl.txt"
    )
    config.write_text(yaml.safe_dump(data))
    proc = run(repo, config, "Task", "--pr")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    path, state = _state(repo)
    assert not ({"pr", "publish", "pull_request"} & state.keys()), state
    config.unlink()
    ready.touch()
    proc = stargate(repo, "resume", state["run_id"], config_home=root / "config-home")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not (root / "pr-args").exists(), proc.stdout + proc.stderr
    assert git_output(repo, "ls-remote", "--heads", "origin") == ""
    proc = stargate(repo, "resume", state["run_id"], "--pr", config_home=root / "config-home")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    resumed = json.loads(path.read_text())
    assert git_output(repo, "ls-remote", "--heads", "origin").startswith(resumed["commit"])
    assert (root / "pr-args").read_text().splitlines() == [state["branch"], "Task"]


def test_an_existing_remote_branch_is_refused_before_push_and_keeps_the_result(root: Path) -> None:
    repo = make_repo(root)
    bare = _origin(root, repo)
    config = root / "config.yaml"
    write_config(config, 'echo "VERDICT: APPROVED"', test_command="true",
                 pull_request=_command(root))
    proc = run(repo, config)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    path, state = _state(repo)
    git_output(repo, "push", "origin", state["branch"])
    proc = stargate(repo, "resume", state["run_id"], "--pr", config_home=root / "config-home")
    assert proc.returncode == 6, proc.stdout + proc.stderr
    assert "already exists" in proc.stderr, proc.stdout + proc.stderr
    assert not (path.parent / "pull-request-push.log").exists(), proc.stdout + proc.stderr
    assert not (root / "pr-args").exists(), proc.stdout + proc.stderr
    assert git_output(bare, "rev-parse", state["branch"]) == state["commit"]
    resumed = json.loads(path.read_text())
    assert resumed["status"] == "approved" and resumed["commit"] == state["commit"], resumed
    assert "Verdict: APPROVED" in (path.parent / "summary.md").read_text()


def test_no_remote_or_an_unqueryable_remote_cannot_push_or_destroy_the_result(root: Path) -> None:
    for remote, expected in ((False, "no git remote"), (True, "Could not query remote")):
        case = root / str(remote)
        case.mkdir()
        repo = make_repo(case)
        if remote:
            git_output(repo, "remote", "add", "origin", str(case / "missing.git"))
        config = case / "config.yaml"
        write_config(config, 'echo "VERDICT: APPROVED"', test_command="true",
                     pull_request=_command(case))
        proc = run(repo, config, "Task", "--pr")
        assert proc.returncode == 6, proc.stdout + proc.stderr
        assert expected in proc.stderr, proc.stdout + proc.stderr
        path, state = _state(repo)
        assert state["status"] == "approved", state
        assert git_output(repo, "rev-parse", state["branch"]) == state["commit"]
        assert not (path.parent / "pull-request-push.log").exists()
        assert not (case / "pr-args").exists()
        assert "pull-request.md" in proc.stdout, proc.stdout + proc.stderr


def test_invalid_publication_requests_fail_before_artifacts_or_agent_calls(root: Path) -> None:
    repo = make_repo(root)
    config, marker = root / "config.yaml", root / "agent-called"
    source_marker = root / "source-called"
    valid = _command(root)
    cases = [
        (None, True, (), "pull_request.command", 1),
        (valid, False, (), "settings.commit", 1),
        (valid, True, ("--no-commit",), "--no-commit", 2),
        ([], True, (), "pull_request", 1),
        ({"command": "pr create"}, True, (), "pull_request.command", 1),
        ({"command": []}, True, (), "pull_request.command", 1),
        ({"command": ["cli", "pr"]}, True, (), "{branch}", 1),
        ({"command": [" ", "{branch}"]}, True, (), "executable", 1),
        ({"command": ["cli", None, "{branch}"]}, True, (), "strings or numbers", 1),
        ({**valid, "env": []}, True, (), "pull_request.env", 1),
    ]
    for block, commit, extra, message, code in cases:
        write_config(config, 'echo "VERDICT: APPROVED"', test_command="true", commit=commit)
        data = yaml.safe_load(config.read_text())
        data["pull_request"] = block
        data["agents"]["noop"]["command"] = agent(f"touch {marker}; echo plan")
        data["task_sources"] = [{"hosts": ["tracker.example"], "commands": [
            agent(f"touch {source_marker}; echo Task"),
        ]}]
        config.write_text(yaml.safe_dump(data))
        for task, source_args in (("Task", ()), ("", ("--from", "https://tracker.example/42"))):
            proc = run(repo, config, task, "--pr", *extra, *source_args)
            assert proc.returncode == code, proc.stdout + proc.stderr
            assert message in proc.stderr, proc.stdout + proc.stderr
            assert not marker.exists(), proc.stdout + proc.stderr
            assert not source_marker.exists(), "invalid --pr must not call the tracker"
            assert not (repo / ".stargate/runs").exists(), proc.stdout + proc.stderr
    assert pull_request_command({}) is None
    assert pull_request_command({"pull_request": {"command": ["cli", "{branch}", 42, 1.5]}}) == [
        "cli", "{branch}", "42", "1.5",
    ]


def test_resume_validates_its_actual_config_before_agents_or_state_changes(root: Path) -> None:
    repo = make_repo(root)
    config = root / "config.yaml"
    write_config(config, 'echo "VERDICT: APPROVED"', test_command="true")
    proc = run(repo, config)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    path, state = _state(repo)
    before = path.read_bytes()
    # A current project block cannot authorize or supply commands to a frozen run.
    (repo / ".stargate.yaml").write_text(yaml.safe_dump({"pull_request": _command(root)}))
    proc = stargate(repo, "resume", state["run_id"], "--pr", config_home=root / "config-home")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "pull_request.command" in proc.stderr, proc.stdout + proc.stderr
    assert path.read_bytes() == before
    frozen = path.parent / "config.yaml"
    data = yaml.safe_load(frozen.read_text())
    data["pull_request"] = _command(root)
    data["settings"]["commit"] = False
    frozen.write_text(yaml.safe_dump(data))
    proc = stargate(repo, "resume", state["run_id"], "--pr", config_home=root / "config-home")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "settings.commit" in proc.stderr, proc.stdout + proc.stderr
    assert path.read_bytes() == before


def test_a_failed_pr_command_keeps_the_pushed_branch_and_gives_a_retry_command(root: Path) -> None:
    repo = make_repo(root)
    bare = _origin(root, repo)
    config = root / "config.yaml"
    write_config(config, 'echo "VERDICT: APPROVED"', test_command="true", pull_request={
        "command": ["/bin/sh", "-c", "echo cannot-open >&2; exit 7", "_", "{branch}"],
    })
    proc = run(repo, config, "Task", "--pr")
    assert proc.returncode == 6, proc.stdout + proc.stderr
    path, state = _state(repo)
    assert state["status"] == "approved", state
    assert git_output(bare, "rev-parse", state["branch"]) == state["commit"]
    assert "cannot-open" in proc.stderr and "already on origin" in proc.stderr, proc.stderr
    assert "branch was pushed to origin" in proc.stdout, proc.stdout
    assert "git push origin" not in proc.stdout, proc.stdout
    assert str(path.parent / "pull-request.md") in proc.stdout, proc.stdout


def test_doctor_checks_the_publish_binary_and_rejects_bad_config_before_probes(root: Path) -> None:
    repo = make_repo(root)
    config, marker = root / "config.yaml", root / "probe-called"
    missing = "stargate-test-missing-pr-binary"
    write_config(config, 'echo "VERDICT: APPROVED"', test_command="true", pull_request={
        "command": [missing, "{branch}", "{title}"],
        "env": {"PR_TOKEN": "secret-value", "PR_REMOVE": None},
    })
    proc = doctor(repo, config)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    for expected in (f"MISSING  {missing}", "Pull request:", "only when you pass --pr",
                     "└─ env:", "PR_TOKEN", "PR_REMOVE (unset)"):
        assert expected in proc.stdout, proc.stdout + proc.stderr
    assert "secret-value" not in proc.stdout, proc.stdout
    data = yaml.safe_load(config.read_text())
    data["pull_request"]["command"] = []
    data["agents"]["noop"] = {"command": agent(f"touch {marker}"), "probe": "Check"}
    config.write_text(yaml.safe_dump(data))
    proc = doctor(repo, config, "--probe")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "pull_request.command" in proc.stdout, proc.stdout + proc.stderr
    assert not marker.exists()


def test_publication_commands_reuse_environment_and_diagnose_execution_failures(root: Path) -> None:
    block = {"command": ["/bin/sh", "-c", 'printf "%s:%s" "$PR_SET" "${PR_REMOVE-absent}"',
                         "_", "{branch}"], "env": {"PR_SET": "configured", "PR_REMOVE": None}}
    config = {"pull_request": block}
    cmd = publication_command(config, "stargate/task", "Title")
    with patch.dict(os.environ, {"PR_SET": "inherited", "PR_REMOVE": "secret"}):
        assert open_pull_request(config, root, cmd, "body") == "configured:absent"
    for failure, expected in (
        (FileNotFoundError("missing-command"), "missing-command"),
        (subprocess.TimeoutExpired(cmd, 300, stderr=b"partial error"), "partial error"),
    ):
        with patch("stargate.publish.subprocess.run", side_effect=failure):
            try:
                open_pull_request(config, root, cmd, "body")
            except StargateError as exc:
                assert expected in str(exc), str(exc)
            else:
                raise AssertionError("command failures must be diagnosed")


def test_push_checks_the_push_destination_and_never_overwrites_a_racing_branch(root: Path) -> None:
    repo = make_repo(root)
    bare = _origin(root, repo)
    destination = root / "destination.git"
    git_output(root, "init", "-q", "--bare", str(destination))
    git_output(repo, "remote", "set-url", "--push", "origin", str(destination))
    branch = "stargate/test"
    git_output(repo, "branch", branch)
    # A branch present only at pushurl must still block before a push is attempted.
    git_output(repo, "push", "origin", branch)
    try:
        push_branch(repo, "origin", branch, root)
    except StargateError as exc:
        assert "already exists" in str(exc), str(exc)
    else:
        raise AssertionError("must query the push destination")
    assert not (root / "pull-request-push.log").exists()
    assert git_output(repo, "ls-remote", "--heads", str(bare)) == ""
    # Simulate ls-remote having observed absence just before another writer created it.
    git_output(repo, "update-ref", f"refs/heads/{branch}", "HEAD")
    (repo / "app.py").write_text("x = 2\n")
    git_output(repo, "commit", "-qam", "next")
    git_output(repo, "branch", "-f", branch, "HEAD")
    original = git_output(destination, "rev-parse", branch)
    from stargate.core import run_process

    def race(cmd, *args, **kwargs):
        if cmd[1] == "ls-remote":
            return subprocess.CompletedProcess(cmd, 0, "")
        return run_process(cmd, *args, **kwargs)

    with patch("stargate.publish.run_process", side_effect=race):
        try:
            push_branch(repo, "origin", branch, root)
        except StargateError as exc:
            assert "Could not push" in str(exc), str(exc)
        else:
            raise AssertionError("a branch created since the query must not be fast-forwarded")
    assert git_output(destination, "rev-parse", branch) == original


def test_publication_diagnostics_hide_credentials_while_git_receives_the_real_url(
    root: Path,
) -> None:
    url = "https://test-user:fake-push-token@host.invalid/repo.git"
    popen = subprocess.Popen
    for operation, failure in (("", ""), ("ls-remote", "exit"), ("push", "exit"),
                               ("ls-remote", "timeout"), ("push", "timeout")):
        calls = []

        def fake_git(cmd, calls=calls, operation=operation, failure=failure, **kwargs):
            # Replace network operations with local shell commands, keeping the real runner.
            calls.append(cmd)
            script = "true"
            if cmd[1] == operation:
                script = "sleep 5" if failure == "timeout" else "echo denied >&2; exit 1"
            return popen(["/bin/sh", "-c", script], **kwargs)

        output, error = io.StringIO(), ""
        with redirect_stdout(output), patch(
            "stargate.publish.git_quiet", side_effect=["main", url],
        ), patch("stargate.core.subprocess.Popen", side_effect=fake_git), patch(
            "stargate.core.HEARTBEAT_SECONDS", 0.01,
        ), patch("stargate.publish.PUBLISH_TIMEOUT_SECONDS", 0.03):
            try:
                push_branch(root, "origin", "stargate/test", root)
            except StargateError as exc:
                error = str(exc)
        assert bool(error) == bool(failure), error
        if failure:
            assert ("timed out" if failure == "timeout" else "denied") in error, error
        expected = ["ls-remote"] if operation == "ls-remote" else ["ls-remote", "push"]
        assert [cmd[1] for cmd in calls] == expected, calls
        assert all(url in cmd for cmd in calls), "Git must receive the original credentials"
        diagnostics = output.getvalue() + error
        for log in root.glob("pull-request-*.log"):
            diagnostics += log.read_text()
        assert "test-user" not in diagnostics and "fake-push-token" not in diagnostics, diagnostics
        assert "$ git ls-remote --heads -- origin refs/heads/stargate/test" in diagnostics
        if "push" in expected:
            assert "$ git push" in diagnostics, diagnostics
            assert "-- origin refs/heads/stargate/test:refs/heads/stargate/test" in diagnostics


def test_remote_selection_refuses_ambiguity_and_push_defaults_cannot_publish_other_refs(
    root: Path,
) -> None:
    repo = make_repo(root)
    bare = _origin(root, repo)
    git_output(repo, "remote", "rename", "origin", "only")
    assert select_remote(repo) == "only"
    git_output(repo, "remote", "add", "second", str(root / "unused.git"))
    try:
        select_remote(repo)
    except StargateError as exc:
        assert "Multiple remotes" in str(exc), str(exc)
    else:
        raise AssertionError("multiple remotes must not be guessed")
    git_output(repo, "remote", "rename", "only", "origin")
    assert select_remote(repo) == "origin"
    git_output(repo, "config", "remote.origin.mirror", "true")
    git_output(repo, "config", "push.followTags", "true")
    git_output(repo, "tag", "-am", "private tag", "private")
    branch = "stargate/test"
    git_output(repo, "branch", branch)
    push_branch(repo, "origin", branch, root)
    refs = git_output(bare, "for-each-ref", "--format=%(refname)")
    assert refs == f"refs/heads/{branch}", refs


def test_fanout_publishes_only_the_approved_integration_branch(root: Path) -> None:
    repo = make_repo(root)
    bare = _origin(root, repo)
    graph, config = root / "graph.json", root / "config.yaml"
    graph.write_text(json.dumps({"name": "small change", "tasks": [
        {"id": "unit", "task": "Create unit.txt", "depends_on": [], "acceptance": ["file exists"]},
    ]}))
    write_fanout_config(config, graph, "echo unit > unit.txt")
    data = yaml.safe_load(config.read_text())
    data["pull_request"] = _command(root)
    config.write_text(yaml.safe_dump(data))
    proc = run(repo, config, "Task title", "--fan-out", "--pr")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    _, state = _state(repo)
    refs = git_output(bare, "for-each-ref", "--format=%(refname)")
    assert refs == f"refs/heads/{state['branch']}", refs
    assert (root / "pr-args").read_text().splitlines() == [state["branch"], "Task title"]
    assert "fixer edits were not copied back to task branches" in proc.stdout, proc.stdout


def test_budget_stops_and_commit_failures_keep_their_exit_codes_without_publishing(
    root: Path,
) -> None:
    for mode, expected_exit in (("budget", 4), ("fanout-budget", 4), ("commit", 5)):
        case = root / mode
        case.mkdir()
        repo = make_repo(case)
        _origin(case, repo)
        config = case / "config.yaml"
        if mode == "fanout-budget":
            graph = case / "graph.json"
            graph.write_text(json.dumps({"name": "budget stop", "tasks": [
                {"id": "unit", "task": "Create unit.txt", "depends_on": [],
                 "acceptance": ["file exists"]},
            ]}))
            write_fanout_config(config, graph, "echo unit > unit.txt; echo tokens 10")
        else:
            write_config(config, 'echo "VERDICT: APPROVED"', test_command="true")
        data = yaml.safe_load(config.read_text())
        data["pull_request"] = _command(case)
        if "budget" in mode:
            data["settings"]["max_task_tokens"] = 1
            data["agents"]["dev"]["command"] = agent("echo change >> impl.txt; echo tokens 10")
            data["agents"]["dev"]["usage_pattern"] = r"tokens (\d+)"
        else:
            hook = repo / ".git/hooks/pre-commit"
            hook.write_text("#!/bin/sh\nexit 1\n")
            hook.chmod(0o755)
        config.write_text(yaml.safe_dump(data))
        extra = ("--fan-out",) if mode == "fanout-budget" else ()
        proc = run(repo, config, "Task", "--pr", *extra)
        assert proc.returncode == expected_exit, proc.stdout + proc.stderr
        path, state = _state(repo)
        assert state["status"] == ("budget_exceeded" if "budget" in mode else "approved"), state
        assert "To publish it yourself:" in proc.stdout, proc.stdout + proc.stderr
        assert (path.parent / "pull-request.md").exists()
        assert not (path.parent / "pull-request-remote.log").exists()
        assert not (case / "pr-args").exists()
        assert git_output(repo, "ls-remote", "--heads", "origin") == ""
        if mode == "fanout-budget":
            assert (
                "No integration terminal commit was created; "
                "1 completed task commit remains on the task branches."
            ) in proc.stdout, proc.stdout + proc.stderr
        elif mode == "commit":
            assert "Nothing was committed" in proc.stdout, proc.stdout + proc.stderr


def test_remote_warnings_do_not_masquerade_as_an_existing_branch(root: Path) -> None:
    repo = make_repo(root)
    bare = _origin(root, repo)
    branch = "stargate/warnings"
    git_output(repo, "branch", branch)
    real_git = shutil.which("git")
    assert real_git
    binaries = root / "bin"
    binaries.mkdir()
    wrapper = binaries / "git"
    wrapper.write_text(
        '#!/bin/sh\nif [ "$1" = ls-remote ]; then\n'
        "  echo 'warning: redirecting to a repository' >&2\n"
        "  echo 'Warning: Permanently added host to known hosts.' >&2\n"
        f'fi\nexec {shlex.quote(real_git)} "$@"\n'
    )
    wrapper.chmod(0o755)
    with patch.dict(os.environ, {"PATH": str(binaries) + os.pathsep + os.environ["PATH"]}):
        push_branch(repo, "origin", branch, root)
        assert git_output(bare, "rev-parse", branch) == git_output(repo, "rev-parse", branch)
        assert "warning: redirecting" in (root / "pull-request-remote.log").read_text()
        (root / "pull-request-push.log").unlink()
        try:
            push_branch(repo, "origin", branch, root)
        except StargateError as exc:
            assert "already exists" in str(exc), str(exc)
        else:
            raise AssertionError("a real branch must still be refused alongside warnings")
        assert not (root / "pull-request-push.log").exists()


def test_publication_config_errors_cannot_escape_and_replace_a_finished_verdict(root: Path) -> None:
    repo = make_repo(root)
    config = root / "config.yaml"
    write_config(config, 'echo "VERDICT: APPROVED"', test_command="true")
    proc = run(repo, config)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    path, state = _state(repo)
    ctx = load_run(repo, state["run_id"], {}, use_frozen=True)
    for block in (None, {"command": []}):
        ctx.config["pull_request"] = block
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error), patch(
            "stargate.stages.select_remote",
        ) as select:
            code = finish(ctx, ctx.task, "APPROVED", 0, commit=False, publish=True)
        assert code == 6, output.getvalue() + error.getvalue()
        assert "pull_request.command" in error.getvalue(), error.getvalue()
        select.assert_not_called()
        after = json.loads(path.read_text())
        assert after["status"] == "approved" and after["commit"] == state["commit"], after
        assert "Verdict: APPROVED" in (path.parent / "summary.md").read_text()
        assert "- **Verdict:** APPROVED" in (path.parent / "pull-request.md").read_text()


def test_a_blank_task_uses_a_nonempty_fallback_title_before_opening_the_pr(root: Path) -> None:
    repo = make_repo(root)
    config = root / "config.yaml"
    write_config(config, 'echo "VERDICT: APPROVED"', test_command="true",
                 pull_request=_command(root))
    proc = run(repo, config)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    _, state = _state(repo)
    ctx = load_run(repo, state["run_id"], {}, use_frozen=True)
    for task in ("", " \n\t"):
        ctx.task = task
        with patch("stargate.stages.select_remote", return_value="origin"), patch(
            "stargate.stages.push_branch",
        ):
            pushed, output, code = _publish(ctx, "APPROVED", 0, "empty")
        assert pushed == "origin" and code is None, output
    assert (root / "pr-args").read_text().splitlines() == [
        state["branch"], "stargate run", state["branch"], "stargate run",
    ]
