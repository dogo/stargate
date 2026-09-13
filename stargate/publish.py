"""Push only the run's branch; the configured command opens the pull request."""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

from .config import agent_env, pull_request_command
from .core import StargateError, git_quiet, run_process

# ponytail: fixed timeout; make configurable if publication needs longer.
PUBLISH_TIMEOUT_SECONDS = 300
PUBLISH_FAILED = 6


def select_remote(repo: Path) -> str:
    remotes = git_quiet(repo, "remote").splitlines()
    if not remotes:
        raise StargateError("This repository has no git remote, so there is nowhere to push.")
    if "origin" in remotes:
        return "origin"
    if len(remotes) == 1:
        return remotes[0]
    raise StargateError(
        f"Multiple remotes without origin ({', '.join(remotes)}); open the PR by hand."
    )


def push_branch(repo: Path, remote: str, branch: str, artifacts: Path) -> None:
    if not branch.startswith("stargate/") or branch == git_quiet(
        repo, "rev-parse", "--abbrev-ref", "HEAD"
    ).strip():
        raise StargateError("Refusing to publish anything but the run's own stargate/* branch.")
    # Query the destination we will actually push to, including pushurl overrides.
    urls = git_quiet(repo, "remote", "get-url", "--push", "--all", remote).splitlines()
    if len(urls) != 1:
        raise StargateError(
            f"Remote {remote!r} needs exactly one push destination; refusing to push."
        )
    ref = f"refs/heads/{branch}"
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    probe = run_process(
        ["git", "ls-remote", "--heads", "--", urls[0], ref], repo,
        check=False, timeout=PUBLISH_TIMEOUT_SECONDS, env=env,
        log_path=artifacts / "pull-request-remote.log",
        display_args=["git", "ls-remote", "--heads", "--", remote, ref],
    )
    if probe.returncode:
        raise StargateError(
            f"Could not query remote {remote!r} (git ls-remote exited {probe.returncode}); "
            f"refusing to push.\n{probe.stdout[-4000:]}"
        )
    # The log combines stdout and stderr; warnings are not advertised refs.
    if any(line.endswith(f"\t{ref}") for line in probe.stdout.splitlines()):
        raise StargateError(f"{branch!r} already exists on {remote!r}; refusing to push.")
    # An empty expected ref permits creation only, even if the branch appears
    # after ls-remote. It cannot force-update an existing branch. Explicit URL,
    # refspec and no-follow-tags also bypass remote mirror/push defaults.
    proc = run_process(
        ["git", "push", "--no-follow-tags", f"--force-with-lease={ref}:",
         "--", urls[0], f"{ref}:{ref}"], repo,
        check=False, timeout=PUBLISH_TIMEOUT_SECONDS, env=env,
        log_path=artifacts / "pull-request-push.log",
        display_args=["git", "push", "--no-follow-tags", f"--force-with-lease={ref}:",
                      "--", remote, f"{ref}:{ref}"],
    )
    if proc.returncode:
        raise StargateError(
            f"Could not push {branch!r} to {remote!r} (exit {proc.returncode}).\n"
            f"{proc.stdout[-4000:]}"
        )


def publication_command(config: dict[str, Any], branch: str, title: str) -> list[str]:
    argv = pull_request_command(config)
    if argv is None:
        raise StargateError("--pr needs a pull_request.command in the run's config.")
    values = {"branch": branch, "title": title}
    # One pass keeps placeholder-looking text in the task literal.
    return [re.sub(r"\{(branch|title)\}", lambda m: values[m[1]], part) for part in argv]


def open_pull_request(config: dict[str, Any], repo: Path, cmd: list[str], body: str) -> str:
    try:
        proc = subprocess.run(
            cmd, cwd=repo, text=True, input=body, capture_output=True,
            timeout=PUBLISH_TIMEOUT_SECONDS, env=agent_env(config["pull_request"]),
        )
    except subprocess.TimeoutExpired as exc:
        stderr = exc.stderr or b""
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        raise StargateError(
            f"{shlex.join(cmd)} timed out after {PUBLISH_TIMEOUT_SECONDS}s: {stderr[-4000:]}"
        ) from exc
    except (OSError, ValueError) as exc:
        raise StargateError(f"Could not run {shlex.join(cmd)}: {exc}") from exc
    if proc.returncode:
        raise StargateError(
            f"{shlex.join(cmd)} exited {proc.returncode}: {proc.stderr[-4000:]}"
        )
    return proc.stdout
