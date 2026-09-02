"""Errors, the run's shared state, and the two things everything shells out to:
a subprocess runner that streams and stays killable, and git."""
from __future__ import annotations

import contextlib
import re
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class StargateError(RuntimeError):
    pass


class Terminated(KeyboardInterrupt):
    """A terminating signal that follows the already-safe interrupt path."""

    def __init__(self, signum: int) -> None:
        super().__init__(f"signal {signum} ({signal.Signals(signum).name})")
        self.signum = signum


@dataclass(frozen=True)
class Detected:
    command: str
    source: str


@dataclass
class RunContext:
    repo: Path
    config: dict[str, Any]
    run_id: str
    slug: str
    branch: str
    base_ref: str
    base_commit: str
    worktree: Path
    artifacts: Path
    task: str = ""
    stage: str = "init"
    done: set[str] = field(default_factory=set)
    tokens_used: int = 0
    tag: str = ""
    named_by_user: bool = False
    test_command: str = ""
    test_source: str = ""
    detected: list[Detected] = field(default_factory=list)
    test_artifacts: set[str] = field(default_factory=set)
    commit: str = ""
    commit_error: str = ""


# How often a running agent prints that it is still alive.
HEARTBEAT_SECONDS = 30


# Cleanup must not turn a terminating signal into an indefinite wait.
KILL_GRACE_SECONDS = 10


def run_process(
    args: list[str],
    cwd: Path,
    *,
    capture: bool = True,
    check: bool = True,
    timeout: float | None = None,
    log_path: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    print(f"\n$ {shlex.join(args)}", flush=True)
    if log_path is not None:
        # Straight to disk, so a silent multi-minute agent can be tailed live
        # instead of surfacing only once the process exits.
        with log_path.open("w") as handle:
            proc = subprocess.Popen(
                args, cwd=str(cwd), text=True, stdout=handle,
                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env,
            )
            started = time.monotonic()
            deadline = None if timeout is None else started + timeout
            try:
                while True:
                    try:
                        proc.wait(timeout=HEARTBEAT_SECONDS)
                        break
                    except subprocess.TimeoutExpired:
                        pass
                    if deadline is not None and time.monotonic() > deadline:
                        proc.kill()
                        proc.wait()
                        raise StargateError(
                            f"Command timed out after {timeout}s "
                            f"(partial trace in {log_path}): {shlex.join(args)}"
                        )
                    # Growing byte count is the "still moving, not hung" signal;
                    # the trace itself stays out of the terminal.
                    size = log_path.stat().st_size if log_path.exists() else 0
                    elapsed = time.monotonic() - started
                    print(
                        f"  ... {elapsed:.0f}s elapsed, {size:,} bytes written",
                        flush=True,
                    )
            except BaseException:
                # A signal reaches the orchestrator, not necessarily the agent.
                # Leaving it alive would let it keep editing during a resume.
                with contextlib.suppress(OSError):
                    proc.kill()
                with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                    proc.wait(timeout=KILL_GRACE_SECONDS)
                raise
        output = log_path.read_text() if log_path.exists() else ""
        if check and proc.returncode != 0:
            raise StargateError(
                f"Command failed with exit code {proc.returncode} "
                f"(trace in {log_path}): {shlex.join(args)}"
            )
        return subprocess.CompletedProcess(args, proc.returncode, output, None)
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.STDOUT if capture else None,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise StargateError(
            f"Command timed out after {timeout}s: {shlex.join(args)}"
        ) from exc
    if capture and proc.stdout:
        print(proc.stdout, end="" if proc.stdout.endswith("\n") else "\n", flush=True)
    if check and proc.returncode != 0:
        raise StargateError(
            f"Command failed with exit code {proc.returncode}: {shlex.join(args)}"
        )
    return proc


def git(repo: Path, *args: str, capture: bool = True, check: bool = True):
    return run_process(["git", *args], repo, capture=capture, check=check)


def repo_root(start: Path) -> Path:
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=str(start),
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise StargateError("Current directory is not inside a Git repository.")
    return Path(proc.stdout.strip()).resolve()


def slugify(text: str, max_len: int = 42) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return (text or "task")[:max_len].rstrip("-")


BRANCH_NAME_WORDS = 5


BRANCH_NAME_CHARS = 32


def short_name(text: str) -> str:
    """Return a short slug made only from whole words."""
    words = [word.lower() for word in re.findall(r"[a-zA-Z0-9]+", text)]
    kept: list[str] = []
    for word in words[:BRANCH_NAME_WORDS]:
        candidate = "-".join([*kept, word])
        if len(candidate) > BRANCH_NAME_CHARS:
            break
        kept.append(word)
    return "-".join(kept)


NAME_PREFIX = "NAME:"


def split_plan_name(plan: str) -> tuple[str, str]:
    """Extract an optional first-line name without damaging older plans.

    Only a valid name on the first non-empty line is removed. Custom and frozen
    prompts predate this contract, and an ignored or malformed instruction must
    keep flowing verbatim instead of turning a compatible run into a failure.
    """
    lines = plan.splitlines()
    first = next((index for index, line in enumerate(lines) if line.strip()), None)
    if first is None:
        return "", plan
    line = lines[first].strip()
    if not line.startswith(NAME_PREFIX):
        return "", plan
    name = short_name(line[len(NAME_PREFIX):].strip())
    if not name:
        return "", plan
    return name, "\n".join([*lines[:first], *lines[first + 1:]]).strip()
