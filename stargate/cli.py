#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .config import (
    AGENT_RETRIES_DEFAULT,
    AGENT_RETRY_BACKOFF_DEFAULT,
    TEST_COMMAND_PLACEHOLDER,
    PROJECT_CONFIG,
    ROLES,
    agent_command,
    agent_entry,
    agent_env,
    env_summary,
    expand_test_command,
    find_prompt,
    init_config,
    init_prompts,
    load_config,
    parse_usage,
    prompt_dirs,
    render_prompt,
    resolve_config,
    retry_settings,
    test_command_grant,
    token_cap,
)
from .core import (
    Detected,
    RunContext,
    StargateError,
    Terminated,
    git,
    repo_root,
    run_process,
    short_name,
    slugify,
    split_plan_name,
)


def resolve_base_ref(repo: Path, requested: str | None) -> tuple[str, str]:
    if requested:
        ref = requested
    else:
        proc = git(repo, "rev-parse", "--abbrev-ref", "HEAD", capture=True)
        ref = proc.stdout.strip()
        if ref == "HEAD":
            ref = git(repo, "rev-parse", "HEAD").stdout.strip()

    commit = git(
        repo, "rev-parse", "--verify", "--end-of-options",
        f"{ref}^{{commit}}", capture=True,
    ).stdout.strip()

    symbolic = subprocess.run(
        ["git", "rev-parse", "--symbolic-full-name", "--verify",
         "--end-of-options", ref],
        cwd=str(repo), text=True, capture_output=True,
    )
    local_branch = symbolic.stdout.strip()
    if symbolic.returncode == 0 and local_branch.startswith("refs/heads/"):
        branch = local_branch.removeprefix("refs/heads/")
        remote = subprocess.run(
            ["git", "config", "--get", f"branch.{branch}.remote"],
            cwd=str(repo), text=True, capture_output=True,
        ).stdout.strip()
        merge_ref = subprocess.run(
            ["git", "config", "--get", f"branch.{branch}.merge"],
            cwd=str(repo), text=True, capture_output=True,
        ).stdout.strip()
        if remote and merge_ref:
            upstream = (
                merge_ref if remote == "."
                else f"{remote}/{merge_ref.removeprefix('refs/heads/')}"
            )
            if remote == ".":
                upstream_proc = subprocess.run(
                    ["git", "rev-parse", "--verify", "--end-of-options",
                     f"{merge_ref}^{{commit}}"],
                    cwd=str(repo), text=True, capture_output=True,
                )
            else:
                try:
                    upstream_proc = subprocess.run(
                        ["git", "ls-remote", "--exit-code", "--refs",
                         remote, merge_ref],
                        cwd=str(repo), text=True, capture_output=True,
                        stdin=subprocess.DEVNULL, timeout=60,
                        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                    )
                except subprocess.TimeoutExpired as exc:
                    raise StargateError(
                        f"Timed out validating upstream {upstream!r}."
                    ) from exc
            if upstream_proc.returncode != 0 or not upstream_proc.stdout.strip():
                detail = upstream_proc.stderr.strip() or "ref not found"
                raise StargateError(
                    f"Could not validate upstream {upstream!r}: {detail}"
                )
            upstream_commit = upstream_proc.stdout.split()[0]
            if upstream_commit != commit:
                contains_upstream = subprocess.run(
                    ["git", "merge-base", "--is-ancestor", upstream_commit,
                     commit],
                    cwd=str(repo), stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                ).returncode == 0
                if not contains_upstream:
                    raise StargateError(
                        f"Base branch {ref!r} is behind or diverged from "
                        f"upstream {upstream!r}. Update it before running "
                        "Stargate."
                    )
    return ref, commit


def warn_if_dirty(repo: Path) -> None:
    status = git(repo, "status", "--porcelain").stdout.strip()
    if status:
        print(
            "\nWarning: the source repository has local changes. "
            "The agent worktree is created from the selected base ref, "
            "so those uncommitted source-tree changes are NOT copied.",
            file=sys.stderr,
        )


PROBE_TIMEOUT_DEFAULT = 120
FINGERPRINT_LINES = 20
PROBE_CAPABILITIES = ("read", "write")


@dataclass
class Capability:
    """A file operation that a probe must demonstrate, not merely describe."""

    kind: str
    path: Path
    marker: str = ""


def unique_agents(config: dict[str, Any]) -> dict[Any, tuple[list[str], Any, dict[str, Any]]]:
    """Distinct agent invocations: the four default roles map onto two commands,
    and probing per role would bill twice for nothing.

    Identity is command AND environment. Two roles running the same command
    under different credentials are two different things to verify -- deduping
    on the command alone would report one of them without ever calling it.
    Keeping the entry that declared the probe also keeps its prompt and
    capability expectation together when only a later duplicate declares one.
    """
    agents: dict[Any, tuple[list[str], Any, dict[str, Any]]] = {}
    for role in ROLES:
        entry = agent_entry(config, role)
        declared = entry.get("env") or {}
        key = (
            tuple(agent_command(config, role)),
            tuple(sorted((str(k), v) for k, v in declared.items())) if isinstance(declared, dict) else None,
        )
        names, prober, first = agents.get(key, ([], None, entry))
        names.append(config["workflow"][role])
        agents[key] = (
            names,
            prober if prober is not None else (
                entry if entry.get("probe") is not None else None
            ),
            first,
        )
    return agents


def probe_one(command: tuple[str, ...], prompt: str, cwd: Path, output: Path,
              timeout: float | None, env: dict[str, str] | None,
              test_command: str,
              capability: Capability | None = None) -> str:
    """Empty string on success, otherwise the reason it failed."""
    writes_final = any("{output}" in part for part in command)
    cmd = [part.replace("{output}", str(output)) for part in command]
    cmd = expand_test_command(cmd, test_command)
    try:
        proc = subprocess.run(
            [*cmd, prompt], cwd=cwd, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return f"probe timed out after {timeout}s"
    except OSError as exc:
        return str(exc)

    if proc.returncode:
        return proc.stdout.strip() or f"agent exited with status {proc.returncode}"
    # Exit 0 while writing nothing to {output} is the false positive this flag
    # exists to remove: invoke_agent would kill the run at the first real stage.
    if writes_final:
        if not (output.read_text() if output.exists() else "").strip():
            return ("agent declares {output} but wrote nothing; check that its "
                    "CLI supports the configured flag")
    if capability is None:
        return ""
    if capability.kind == "write":
        if not capability.path.is_file() or not capability.path.stat().st_size:
            return (
                f"agent exited 0 but did not write {capability.path.name}; "
                "its file-editing tools are not working"
            )
        return ""

    # A prose-only answer cannot guess this marker, so returning it proves the
    # role actually used its file-reading tools in the isolated repository.
    answer = (
        output.read_text() if output.exists() else ""
    ) if writes_final else proc.stdout
    if capability.marker not in answer:
        return (
            "agent exited 0 but did not return the marker seeded in "
            f"{capability.path.name}; its file-reading tools are not working"
        )
    return ""


def probe_agents(config: dict[str, Any], test_command: str) -> bool:
    """Make one real, billable call per distinct agent. Opt-in only."""
    print("\nAgent probes:")
    git_bin = shutil.which("git")
    if not git_bin:
        print("  SKIP probes (git is required for the isolated probe directory)")
        return False

    settings = config.get("settings", {})
    timeout = float(settings.get("probe_timeout_seconds", PROBE_TIMEOUT_DEFAULT)) or None
    ok = True
    with tempfile.TemporaryDirectory(prefix="stargate-doctor-") as tmp:
        cwd = Path(tmp)
        try:
            # Probes run outside the repo: the default agents are
            # --sandbox workspace-write, and codex refuses a non-git directory.
            subprocess.run([git_bin, "init", "-q"], cwd=cwd, check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            detail = (getattr(exc, "stdout", None) or str(exc)).strip()
            print("  FAIL probe setup")
            print("       " + detail.replace("\n", "\n       "))
            return False

        for index, (key, (names, prober, entry)) in enumerate(
            unique_agents(config).items()
        ):
            command = key[0]
            label = ", ".join(dict.fromkeys(names))
            if overrides := env_summary(entry):
                label += f" (env: {overrides})"
            if prober is None:
                if entry.get("probe_expect") is not None:
                    print(f"  FAIL {label} (probe_expect needs a probe prompt)")
                    ok = False
                    continue
                print(f"  SKIP {label} (no probe configured)")
                continue
            prompt = prober.get("probe")
            if not isinstance(prompt, str) or not prompt.strip():
                print(f"  FAIL {label} (probe must be a non-empty string)")
                ok = False
                continue
            capability = None
            expect = prober.get("probe_expect")
            probe_path = cwd / f"probe-{index}.txt"
            if expect is not None:
                if expect not in PROBE_CAPABILITIES:
                    print(
                        f"  FAIL {label} (probe_expect must be one of "
                        f"{', '.join(PROBE_CAPABILITIES)})"
                    )
                    ok = False
                    continue
                marker = f"stargate-{uuid.uuid4().hex[:12]}"
                if expect == "read":
                    probe_path.write_text(marker + "\n")
                capability = Capability(expect, probe_path, marker)
                label += f" ({expect})"
                prompt = prompt.replace("{probe_file}", str(probe_path))
            started = time.monotonic()
            error = probe_one(
                command, prompt, cwd, cwd / f"output-{index}.txt", timeout,
                agent_env(entry), test_command, capability,
            )
            print(f"  {'FAIL' if error else 'OK':4} {label} [{time.monotonic() - started:.1f}s]")
            if error:
                print("       " + error.replace("\n", "\n       "))
                ok = False
    return ok


def value_source(
    layers: list[tuple[Path, dict[str, Any]]], section: str, key: str
) -> str:
    """The numbered layer that supplied one effective value."""
    for index, (_, data) in enumerate(layers, 1):
        block = data.get(section)
        if isinstance(block, dict) and key in block:
            return f"[{index}]"
    return "(default)"


DETECTION_MODES = ("report", "auto", "off")
DETECTION_READ_LIMIT = 65536


# A detected command is input the user never wrote. It may run unattended after
# every fixer, influence the review verdict and set the process exit code; a bad
# guess can also be a deploy-ish Make target or a watcher that only ends at the
# timeout. The default therefore fixes the dangerous silence by reporting the
# guess and its evidence, without executing it. Users who accept that risk can
# opt into `auto`; `off` records that the repository intentionally has no test.
def detection_mode(config: dict[str, Any]) -> str:
    settings = config.get("settings", {})
    mode = str(settings.get("test_command_detection", "report") or "report")
    mode = mode.strip().lower()
    if mode not in DETECTION_MODES:
        raise StargateError(
            "settings.test_command_detection must be one of report, auto, off"
        )
    return mode


# Committing is ON by default. The friction this removes is a default-level
# problem: a run whose work exists only as a dirty worktree cannot be built on
# without a hand-made branch and commit, and a `git worktree remove --force`
# destroys it, because the artifacts hold traces and prose, not code. A flag
# nobody knows exists does not fix that. Unlike a detected test command, this
# executes nothing the user has not already sanctioned: the commit lands on a
# branch stargate created, in a worktree stargate created, and is never pushed
# or merged. The documented behaviour it changes is narrow -- `git status` in
# the worktree goes clean, while `git diff <base>` still shows every change.
# `commit: false` restores the old behaviour exactly.
def commit_enabled(config: dict[str, Any]) -> bool:
    value = config.get("settings", {}).get("commit", True)
    if not isinstance(value, bool):
        raise StargateError("settings.commit must be true or false.")
    return value


def read_detection_file(path: Path) -> str | None:
    """Read enough evidence for detection without trusting project file size."""
    try:
        with path.open(encoding="utf-8") as handle:
            return handle.read(DETECTION_READ_LIMIT)
    except (OSError, UnicodeError, ValueError):
        return None


def is_file(path: Path) -> bool:
    with contextlib.suppress(OSError):
        return path.is_file()
    return False


def detect_test_commands(root: Path) -> list[Detected]:
    """Return all likely commands in a deliberate, user-visible priority."""
    detected: list[Detected] = []

    # A hand-written project entry point commonly wraps the native runner, so
    # it outranks language metadata. All lower-ranked matches are still shown.
    for name in ("Makefile", "makefile", "GNUmakefile"):
        text = read_detection_file(root / name)
        if text is not None and re.search(r"(?m)^test\s*:(?!=)", text):
            detected.append(Detected("make test", f"{name}: test target"))
            break

    package = read_detection_file(root / "package.json")
    if package is not None:
        with contextlib.suppress(json.JSONDecodeError, TypeError, AttributeError):
            script = json.loads(package).get("scripts", {}).get("test", "")
            script = script.strip() if isinstance(script, str) else ""
            if script and "error: no test specified" not in script.lower():
                runner = (
                    "pnpm" if is_file(root / "pnpm-lock.yaml")
                    else "yarn" if is_file(root / "yarn.lock")
                    else "npm"
                )
                detected.append(Detected(
                    f"{runner} test", "package.json: scripts.test"
                ))

    for filename, command in (
        ("Cargo.toml", "cargo test"),
        ("go.mod", "go test ./..."),
        ("Package.swift", "swift test"),
    ):
        if is_file(root / filename):
            detected.append(Detected(command, filename))

    pytest_source = ""
    pyproject = read_detection_file(root / "pyproject.toml")
    if pyproject is not None:
        if re.search(r"(?im)^\s*\[tool\.pytest(?:\.|\])", pyproject):
            pytest_source = "pyproject.toml: tool.pytest"
        elif (
            re.search(r"(?im)^\s*pytest(?:[-_.][\w.-]+)?\s*=", pyproject)
            or re.search(
                r'''(?i)["']pytest(?:[-_.][\w.-]+)?(?:[<>=~!][^"']*)?["']''',
                pyproject,
            )
        ):
            pytest_source = "pyproject.toml: pytest dependency"
    if not pytest_source and is_file(root / "pytest.ini"):
        pytest_source = "pytest.ini"
    if not pytest_source:
        for filename in ("setup.cfg", "tox.ini"):
            text = read_detection_file(root / filename)
            if text is not None and re.search(r"(?im)^\s*\[tool:pytest\]", text):
                pytest_source = f"{filename}: tool:pytest"
                break
    if not pytest_source:
        tests = root / "tests"
        with contextlib.suppress(OSError):
            if tests.is_dir() and any(path.is_file() for path in tests.rglob("test_*.py")):
                pytest_source = "tests/: test_*.py"
    # A root-level test_*.py is not enough evidence: this repository's smoke
    # test takes a custom argument and pytest collection would fail. That false
    # positive is exactly why detection reports rather than runs by default.
    if pytest_source:
        detected.append(Detected("pytest -q", pytest_source))

    return detected


def selected_test_command(
    config: dict[str, Any], root: Path
) -> tuple[str, list[Detected]]:
    """The command stargate would run, plus every detected candidate.

    Report-only detection intentionally grants nothing: its candidate is input
    the user has not approved, so letting an agent execute it would defeat the
    mode even if the orchestrator itself abstained.
    """
    settings = config.get("settings", {})
    configured = str(settings.get("test_command", "") or "").strip()
    if configured:
        return configured, []
    mode = detection_mode(config)
    if mode == "off":
        return "", []
    detected = detect_test_commands(root)
    if mode == "auto" and detected:
        return detected[0].command, detected
    return "", detected


def doctor(
    config: dict[str, Any],
    layers: list[tuple[Path, dict[str, Any]]],
    script_dir: Path,
    *,
    probe: bool = False,
    explicit_config: bool = False,
) -> int:
    print("stargate doctor\n")
    qualifier = (
        "explicit --config; used exactly as given"
        if explicit_config else "most specific first"
    )
    print(f"Config sources ({qualifier}):")
    packaged_path = (script_dir / "agents.yaml").resolve()
    for index, (path, _) in enumerate(layers, 1):
        suffix = " (packaged defaults)" if path == packaged_path else ""
        print(f"  [{index}] {path}{suffix}")
    print()
    settings = config.get("settings", {})
    mode = detection_mode(config)
    commit_enabled(config)
    configured_test_command = str(
        settings.get("test_command", "") or ""
    ).strip()
    test_command, candidates = selected_test_command(config, Path.cwd())
    commands = {
        role: expand_test_command(agent_command(config, role), test_command)
        for role in ROLES
    }
    ok = True
    binaries = {"git"}
    for role in ROLES:
        binaries.add(commands[role][0])

    for binary in sorted(binaries):
        path = shutil.which(binary)
        state = "FOUND" if path else "MISSING"
        print(f"{state:8} {binary:12} {path or ''}")
        ok = ok and bool(path)
    print(
        "\nFOUND means the executable is on PATH. Authentication, credits, quota\n"
        "and model availability are NOT checked -- an agent can still fail on its\n"
        "first call (e.g. \"Credit balance is too low\")."
    )

    if probe:
        ok = probe_agents(config, test_command) and ok

    packaged = yaml.safe_load((script_dir / "agents.yaml").read_text()) or {}
    mine, theirs = config.get("version"), packaged.get("version")
    if mine is not None and theirs is not None and mine != theirs:
        print(
            f"\nWARN     config version {mine} differs from the packaged version "
            f"{theirs}.\n         Newer defaults may be missing; compare against "
            f"{script_dir / 'agents.yaml'}."
        )

    print("\nEffective settings:")
    for key, default in (
        ("max_review_loops", 2),
        ("test_command", ""),
        ("test_command_detection", "report"),
        ("commit", True),
        ("max_task_tokens", 0),
        ("agent_timeout_seconds", 1800),
        ("agent_retries", AGENT_RETRIES_DEFAULT),
        ("agent_retry_backoff_seconds", AGENT_RETRY_BACKOFF_DEFAULT),
        ("test_timeout_seconds", 900),
        ("worktree_root", ""),
        ("prompts_dir", ""),
    ):
        value = settings.get(key, default)
        print(f"  {key:22} {value!r}   {value_source(layers, 'settings', key)}")

    print("\nTest command:")
    if configured_test_command:
        source = value_source(layers, "settings", "test_command")
        print(f"  configured  {configured_test_command!r}   {source}")
    elif mode == "off":
        print("  (not configured; detection is off)")
    else:
        print("  (not configured)")
        for candidate in candidates:
            print(f"  detected    {candidate.command:16} {candidate.source}")
        if not candidates:
            print("  No likely project test command detected.")
        elif mode == "auto":
            print(f"  Detection is automatic; {candidates[0].command!r} will run.")
        else:
            print(
                "  Detection is report-only. To use the first candidate, "
                "add to .stargate.yaml:"
            )
            print("    settings:")
            print(f"      test_command: {candidates[0].command!r}")

    cap = token_cap(config)
    print("\nAgents:")
    architect_declares_test_command = False
    for role in ROLES:
        agent_name = config["workflow"][role]
        agent_source = value_source(layers, "agents", agent_name)
        workflow_source = value_source(layers, "workflow", role)
        entry = agent_entry(config, role)
        raw_command = agent_command(config, role)
        declares_test_command = any(
            TEST_COMMAND_PLACEHOLDER in part for part in raw_command
        )
        print(
            f"  {role:10} {agent_source:9} "
            f"{shlex.join(commands[role])}"
        )
        if workflow_source != agent_source:
            print(f"  {'':10} {'':9} └─ role mapped by {workflow_source}")
        if overrides := env_summary(entry):
            print(f"  {'':10} {'':9} └─ env: {overrides}")
        if declares_test_command:
            architect_declares_test_command = (
                architect_declares_test_command or role == "architect"
            )
            if grant := test_command_grant(test_command):
                print(
                    f"  {'':10} {'':9} └─ may run the test command: {grant}"
                )
            elif test_command:
                print(
                    f"  {'':10} {'':9} └─ {{test_command}}: {test_command!r} "
                    "contains a permission-pattern metacharacter or control "
                    "character; it was not interpolated and the grant is dropped"
                )
            else:
                print(
                    f"  {'':10} {'':9} └─ {{test_command}}: no test command "
                    "will run; the grant and its flag are dropped"
                )
        if cap:
            meters = "reports usage" if entry.get("usage_pattern") else "no usage_pattern"
            print(f"  {'':10} {'':9} └─ {meters}")

    if architect_declares_test_command:
        print(
            "\nWARN     the architect declares {test_command}. It runs in your real "
            "repository,\n         not the worktree, so this can grant command "
            "execution there."
        )

    print("\nPrompts:")
    dirs = prompt_dirs(config, script_dir)
    for role in ROLES:
        try:
            print(f"  {role:10} {find_prompt(dirs, role)}")
        except StargateError as exc:
            print(f"  {role:10} MISSING ({exc})")
            ok = False

    return 0 if ok else 1


def record_usage(ctx: RunContext, role: str, transcript: str) -> None:
    """Charge one completed attempt, including one the vendor rejected late."""
    used = parse_usage(
        transcript, agent_entry(ctx.config, role).get("usage_pattern")
    )
    ctx.tokens_used += used
    if used:
        cap = token_cap(ctx.config)
        budget = f" of {cap:,}" if cap else ""
        print(
            f"\n[{role}] reported {used:,} tokens; "
            f"{ctx.tokens_used:,}{budget} used so far."
        )


def attempt_log_path(output_path: Path, attempt: int) -> Path:
    # Attempt one keeps the historical path, so retries-off runs retain the
    # same artifacts while later failures cannot overwrite its evidence.
    suffix = "" if attempt == 1 else f".attempt-{attempt}"
    return output_path.with_name(output_path.name + suffix + ".log")


def failure_fingerprint(error: StargateError, trace: str) -> tuple[str, str]:
    """Normalize per-attempt noise without interpreting vendor error text."""
    # The prefix is ours and distinguishes timeout from a non-zero exit. The
    # rest includes attempt-specific trace paths and the full command, neither
    # of which says whether another attempt has a chance of succeeding.
    reason = str(error).split(" (", 1)[0]
    tail = "\n".join(trace.strip().splitlines()[-FINGERPRINT_LINES:])
    return re.sub(r"\d+", "#", reason), re.sub(r"\d+", "#", tail)


def invoke_agent(
    ctx: RunContext,
    role: str,
    prompt: str,
    cwd: Path,
    output_path: Path,
) -> str:
    """Run one agent and return its FINAL MESSAGE, not its stdout.

    The distinction matters: `codex exec` streams the whole session — reasoning,
    every command it ran, a token footer — to stdout. Forwarding that as {plan}
    or {review} makes each hop pay for the previous hop's trace. An agent whose
    command contains "{output}" is handed a file path to write its last message
    to, and that file is what gets forwarded; its stdout is kept as a .log.

    Retries stay inside this single invocation so no completed role is replayed.
    Each attempt has its own trace, both for debugging and to count any usage
    the failed process reported exactly once.
    """
    cmd = agent_command(ctx.config, role)
    writes_final = any("{output}" in part for part in cmd)
    # Expand {output} first so those literal characters inside a configured
    # test command cannot unexpectedly become a path.
    cmd = [part.replace("{output}", str(output_path)) for part in cmd]
    cmd = expand_test_command(cmd, ctx.test_command)
    timeout = float(ctx.config.get("settings", {}).get("agent_timeout_seconds", 1800))
    env = agent_env(agent_entry(ctx.config, role))
    retries, backoff = retry_settings(ctx.config)
    attempts = retries + 1
    previous_failure: tuple[str, str] | None = None

    for attempt in range(1, attempts + 1):
        log_path = attempt_log_path(output_path, attempt)
        print(f"trace: tail -f {shlex.quote(str(log_path))}", flush=True)

        # A retry or explicitly redone stage must not inherit an earlier
        # answer and pass the output contract after writing nothing.
        if writes_final and output_path.exists():
            output_path.unlink()

        started = time.monotonic()
        try:
            proc = run_process(
                [*cmd, prompt], cwd, timeout=timeout or None, log_path=log_path,
                env=env,
            )
        except OSError as exc:
            # A process that cannot be started will fail the same way after a
            # backoff; unlike an agent exit, it never made a remote request.
            raise StargateError(
                f"Could not start the agent for role '{role}': {exc}"
            ) from exc
        except StargateError as exc:
            if retries:
                trace = log_path.read_text() if log_path.exists() else ""
                record_usage(ctx, role, trace)
                print(
                    f"\n[{role}] attempt {attempt} of {attempts} failed: {exc}",
                    flush=True,
                )
            else:
                # Keeping retries disabled must preserve the original failure
                # path, including terminal output and token accounting.
                raise

            if attempt == attempts:
                raise

            fingerprint = failure_fingerprint(exc, trace)
            if fingerprint == previous_failure:
                remaining = attempts - attempt
                print(
                    f"[{role}] failed identically twice; not retrying "
                    f"{remaining} more time(s).",
                    flush=True,
                )
                raise
            previous_failure = fingerprint

            wait = backoff * 2 ** (attempt - 1)
            print(
                f"[{role}] retrying in {wait:g}s "
                f"(attempt {attempt + 1} of {attempts}).",
                flush=True,
            )
            time.sleep(wait)
            continue

        transcript = proc.stdout or ""
        print(
            f"\n[{role}] exit {proc.returncode} in "
            f"{time.monotonic() - started:.0f}s",
            flush=True,
        )
        record_usage(ctx, role, transcript)
        break

    if not writes_final:
        output_path.write_text(transcript)
        return transcript

    final = output_path.read_text() if output_path.exists() else ""
    if not final.strip():
        raise StargateError(
            f"Agent for role '{role}' declares {{output}} but wrote nothing to "
            f"{output_path}. Check that its CLI supports the flag you passed."
        )
    return final


def default_worktree_root(repo: Path) -> Path:
    return repo.parent / ".stargate-worktrees" / repo.name


def branch_exists(repo: Path, branch: str) -> bool:
    proc = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=str(repo), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return proc.returncode == 0


def unique_branch(repo: Path, branch: str) -> str:
    for attempt in range(1, 100):
        candidate = branch if attempt == 1 else f"{branch}-{attempt}"
        if not branch_exists(repo, candidate):
            return candidate
    raise StargateError(f"Could not find an unused branch name based on {branch!r}.")


def reserve_run(repo: Path, now: str, slug: str) -> tuple[str, str, Path]:
    """Reserve one discriminator for both artifacts and the initial branch.

    A second process in the same second would otherwise share the artifacts
    directory while Git also refuses to attach its branch to another worktree.
    Creating the directory is the atomic claim; old first-attempt names retain
    their exact shape, so `runs` and `resume` need no format migration.
    """
    for attempt in range(1, 100):
        suffix = "" if attempt == 1 else f"-{attempt}"
        tag = f"{now}{suffix}"
        run_id = f"{now}-{slug}{suffix}"
        branch = f"stargate/{slug}-{tag}"
        if branch_exists(repo, branch):
            continue
        artifacts = repo / ".stargate" / "runs" / run_id
        try:
            artifacts.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            continue
        return run_id, branch, artifacts
    raise StargateError(
        f"Could not reserve a unique run name for {slug!r} at {now}."
    )


def make_context(
    repo: Path,
    config: dict[str, Any],
    task: str,
    base_ref: str | None,
    name: str | None = None,
) -> RunContext:
    now = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    named = short_name(name or "") if name is not None else ""
    if name is not None and not named:
        print(
            "Warning: --name produced no usable slug; using the task text.",
            file=sys.stderr,
        )
    slug = named or slugify(task)
    base, base_commit = resolve_base_ref(repo, base_ref)
    run_id, branch, artifacts = reserve_run(repo, now, slug)
    tag = branch.removeprefix(f"stargate/{slug}-")

    configured = str(config.get("settings", {}).get("worktree_root", "") or "").strip()
    worktree_root = Path(os.path.expanduser(configured)).resolve() if configured else default_worktree_root(repo)
    worktree = worktree_root / run_id
    # Keep run artifacts out of the target repo's git status without touching
    # the user's own .gitignore.
    (repo / ".stargate" / ".gitignore").write_text("*\n")
    worktree.parent.mkdir(parents=True, exist_ok=True)

    return RunContext(
        repo=repo,
        config=config,
        run_id=run_id,
        slug=slug,
        branch=branch,
        base_ref=base,
        base_commit=base_commit,
        worktree=worktree,
        artifacts=artifacts,
        task=task,
        tag=tag,
        named_by_user=bool(named),
    )


def budget_spent(ctx: RunContext, next_phase: str) -> bool:
    """Whether the cap is reached. Checked BETWEEN phases: nothing here can stop
    an agent already running, so a single runaway invocation still overshoots."""
    cap = token_cap(ctx.config)
    if not cap or ctx.tokens_used < cap:
        return False
    print(
        f"\nToken budget reached: {ctx.tokens_used:,} of {cap:,} used. "
        f"Stopping before {next_phase}.",
        file=sys.stderr,
    )
    return True


STAGES = ("architect", "worktree", "developer", "review")

# These are the completed records that skip work. The worktree is reused
# regardless, and the review loop already restarts from its first attempt.
REDOABLE_STAGES = ("architect", "developer")


def save_state(ctx: RunContext, status: str, error: str | None = None) -> None:
    """Record where the run got to, so a failed stage can be resumed instead of
    restarting the whole flow and leaving a second plan, branch and worktree."""
    path = ctx.artifacts / "state.json"
    started = json.loads(path.read_text()).get("started_at") if path.exists() else None
    path.write_text(json.dumps({
        "run_id": ctx.run_id,
        "task": ctx.task,
        "repo": str(ctx.repo),
        "base_ref": ctx.base_ref,
        "base_commit": ctx.base_commit,
        "branch": ctx.branch,
        "worktree": str(ctx.worktree),
        "stage": ctx.stage,
        "status": status,
        "error": error,
        "completed": sorted(ctx.done),
        "tokens_used": ctx.tokens_used,
        "named_by_user": ctx.named_by_user,
        "test_artifacts": sorted(ctx.test_artifacts),
        "commit": ctx.commit or None,
        "commit_error": ctx.commit_error or None,
        "started_at": started or dt.datetime.now().isoformat(timespec="seconds"),
        "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
    }, indent=2) + "\n")


def enter_stage(ctx: RunContext, stage: str) -> None:
    ctx.stage = stage
    save_state(ctx, "running")


def complete_stage(ctx: RunContext, stage: str) -> None:
    ctx.done.add(stage)
    save_state(ctx, "running")


def load_run(repo: Path, run_id: str, config: dict[str, Any], use_frozen: bool) -> RunContext:
    artifacts = repo / ".stargate" / "runs" / run_id
    state_path = artifacts / "state.json"
    if not state_path.exists():
        raise StargateError(f"No run state at {state_path}")
    state = json.loads(state_path.read_text())
    frozen = artifacts / "config.yaml"
    if use_frozen and frozen.exists():
        # Default to the run's own frozen config so resuming does not silently
        # change the agents the earlier stages ran under. An explicit --config
        # overrides it, which is how you resume past a bad agent definition.
        config = yaml.safe_load(frozen.read_text()) or config
    return RunContext(
        repo=repo,
        config=config,
        run_id=state["run_id"],
        slug=slugify(state["task"]),
        branch=state["branch"],
        base_ref=state["base_ref"],
        base_commit=str(state.get("base_commit") or state["base_ref"]),
        worktree=Path(state["worktree"]),
        artifacts=artifacts,
        task=state["task"],
        stage=state.get("stage", "init"),
        done=set(state.get("completed", [])),
        tokens_used=int(state.get("tokens_used", 0)),
        test_artifacts=set(state.get("test_artifacts", [])),
        commit=str(state.get("commit") or ""),
        commit_error=str(state.get("commit_error") or ""),
        tag=(
            match.group(1)
            if (match := re.search(
                r"-(\d{8}-\d{6}(?:-\d+)?)$", str(state["branch"])
            )) else ""
        ),
        named_by_user=bool(state.get("named_by_user", False)),
    )


RUN_TASK_WIDTH = 60
RESUMABLE_STATUSES = ("running", "failed")


def read_run(path: Path) -> dict[str, Any]:
    """Build one listing row, including runs whose state cannot be read."""
    state_path = path / "state.json"
    row: dict[str, Any] = {
        "run_id": path.name,
        "status": "unknown",
        "stage": "-",
        "branch": "(unknown)",
        "worktree": "(unknown)",
        "worktree_missing": False,
        "task": "-",
        "updated": "-",
        "resumable": False,
        "error": "",
    }
    try:
        state = json.loads(state_path.read_text())
        if not isinstance(state, dict):
            raise ValueError("state.json is not an object")
    except (OSError, ValueError) as exc:
        row["error"] = f"unreadable state.json ({exc})"
        return row

    status = " ".join(str(state.get("status") or "unknown").split())
    worktree = str(state.get("worktree") or "")
    missing = False
    if worktree:
        try:
            missing = not Path(worktree).exists()
        except (OSError, ValueError):
            missing = True
    row.update(
        run_id=" ".join(str(state.get("run_id") or path.name).split()),
        status=status,
        stage=" ".join(str(state.get("stage") or "-").split()),
        branch=" ".join(str(state.get("branch") or "(unknown)").split()),
        worktree=" ".join(worktree.split()) or "(unknown)",
        worktree_missing=missing,
        updated=" ".join(str(state.get("updated_at") or "-").split()),
        # Tasks are often multi-paragraph input; one row should stay one row.
        task=(" ".join(str(state.get("task") or "").split())[:RUN_TASK_WIDTH] or "-"),
        resumable=status.lower() in RESUMABLE_STATUSES,
        error="",
    )
    return row


def list_runs(repo: Path) -> int:
    root = repo / ".stargate" / "runs"
    # Listing a repository that has never run stargate must not create the
    # bookkeeping directory it is meant only to inspect.
    if not root.is_dir():
        print(f"No recorded runs in {root}")
        return 0

    try:
        paths = sorted(
            (path for path in root.iterdir() if path.is_dir()),
            key=lambda path: path.name,
            reverse=True,
        )
    except OSError as exc:
        raise StargateError(
            f"Could not read recorded runs in {root}: {exc}"
        ) from exc
    if not paths:
        print(f"No recorded runs in {root}")
        return 0

    rows = [read_run(path) for path in paths]
    width = max(len(row["run_id"]) for row in rows)
    print(f"Runs in {repo} (newest first):\n")
    print(f"  {'RUN ID':{width}}  {'STATUS':17} {'STAGE':10} {'UPDATED':19} TASK")
    for row in rows:
        marker = "*" if row["resumable"] else " "
        print(
            f"{marker} {row['run_id']:{width}}  {row['status']:17} "
            f"{row['stage']:10} {row['updated']:19} {row['task']}"
        )
        print(f"    branch    {row['branch']}")
        missing = "  (MISSING)" if row["worktree_missing"] else ""
        print(f"    worktree  {row['worktree']}{missing}")
        if row["error"]:
            print(f"    {row['error']}")

    newest = next((row for row in rows if row["resumable"]), None)
    if newest:
        print(
            "\n* resumable. Resume the newest with: "
            f"stargate resume {newest['run_id']}"
        )
    else:
        print("\nNo runs are marked resumable.")
    return 0


def snapshot(ctx: RunContext, dirs: list[Path]) -> list[Path]:
    """Copy the effective config and all four prompts into the run's artifacts,
    and use those copies for the rest of the run.

    A run outlives its own installation: upgrading or reinstalling the package
    mid-run otherwise deletes the prompts out from under the next role. It also
    makes the run reproducible -- the artifacts say exactly what was used.
    """
    (ctx.artifacts / "config.yaml").write_text(yaml.safe_dump(ctx.config, sort_keys=False))
    frozen = ctx.artifacts / "prompts"
    frozen.mkdir(exist_ok=True)
    for role in ROLES:
        (frozen / f"{role}.md").write_text(find_prompt(dirs, role).read_text())
    return [frozen]


def create_worktree(ctx: RunContext) -> None:
    if ctx.worktree.exists():
        print(f"Reusing existing worktree: {ctx.worktree}")
        return
    exists = git(ctx.repo, "rev-parse", "--verify", ctx.branch, check=False).returncode == 0
    args = ["worktree", "add"] + (
        [str(ctx.worktree), ctx.branch] if exists
        else ["-b", ctx.branch, str(ctx.worktree), ctx.base_commit]
    )
    git(ctx.repo, *args, capture=True)


def git_quiet(repo: Path, *args: str) -> str:
    """Read Git state without burying run output under a full printed diff."""
    proc = subprocess.run(
        ["git", *args], cwd=str(repo), text=True, capture_output=True
    )
    if proc.returncode:
        raise StargateError(
            f"git {' '.join(args)} failed in {repo}: {proc.stderr.strip()}"
        )
    return proc.stdout


def clean_run(repo: Path, run_id: str) -> None:
    root = repo / ".stargate" / "runs"
    if not run_id or Path(run_id).name != run_id or run_id in (".", ".."):
        raise StargateError(f"Invalid run ID: {run_id!r}")
    artifacts = root / run_id
    if artifacts.is_symlink() or not artifacts.is_dir():
        raise StargateError(f"No recorded run {run_id!r} in {root}")
    try:
        state = json.loads((artifacts / "state.json").read_text())
    except (OSError, ValueError) as exc:
        raise StargateError(f"Cannot clean {run_id}: unreadable state.json ({exc})") from exc
    if not isinstance(state, dict) or state.get("run_id") != run_id:
        raise StargateError(f"Cannot clean {run_id}: state.json has a different run ID")
    recorded_repo = state.get("repo")
    if not isinstance(recorded_repo, str) or Path(recorded_repo).resolve() != repo:
        raise StargateError(f"Cannot clean {run_id}: state.json belongs to another repository")
    branch = state.get("branch")
    worktree_value = state.get("worktree")
    if not isinstance(branch, str) or not branch.startswith("stargate/"):
        raise StargateError(f"Cannot clean {run_id}: invalid Stargate branch")
    if not isinstance(worktree_value, str) or not worktree_value:
        raise StargateError(f"Cannot clean {run_id}: invalid worktree path")
    worktree = Path(worktree_value).resolve()
    branch_present = branch_exists(repo, branch)
    worktree_present = worktree.exists()

    if worktree_present:
        checked_out = git_quiet(worktree, "symbolic-ref", "--short", "HEAD").strip()
        if checked_out != branch:
            raise StargateError(
                f"Cannot clean {run_id}: {worktree} has branch {checked_out!r}, "
                f"not {branch!r}"
            )
    elif not branch_present:
        shutil.rmtree(artifacts)
        print(f"Cleaned {run_id}: artifacts removed; worktree and branch were absent.")
        return

    if branch_present:
        merged = subprocess.run(
            ["git", "merge-base", "--is-ancestor", f"refs/heads/{branch}", "HEAD"],
            cwd=str(repo), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if merged.returncode == 1:
            raise StargateError(
                f"Cannot clean {run_id}: branch {branch!r} is not merged into HEAD"
            )
        if merged.returncode != 0:
            raise StargateError(f"Cannot determine whether branch {branch!r} is merged")

    if worktree_present:
        git(repo, "worktree", "remove", str(worktree))
    else:
        git(repo, "worktree", "prune", "--expire", "now")
    if branch_present:
        git(repo, "branch", "-d", "--", branch)
    shutil.rmtree(artifacts)
    print(f"Cleaned {run_id}: worktree, branch and artifacts removed.")


def clean_runs(repo: Path, run_id: str | None, all_runs: bool) -> int:
    if all_runs == (run_id is not None):
        raise StargateError("Use either 'stargate clean <run-id>' or 'stargate clean --all'.")
    if run_id is not None:
        clean_run(repo, run_id)
        return 0

    root = repo / ".stargate" / "runs"
    if not root.is_dir():
        print(f"No recorded runs in {root}")
        return 0
    run_ids = sorted(
        (path.name for path in root.iterdir() if path.is_dir()), reverse=True
    )
    if not run_ids:
        print(f"No recorded runs in {root}")
        return 0
    failures: list[tuple[str, str]] = []
    for candidate in run_ids:
        try:
            clean_run(repo, candidate)
        except StargateError as exc:
            failures.append((candidate, str(exc)))
    if failures:
        for candidate, error in failures:
            print(f"  {candidate}: {error}", file=sys.stderr)
        raise StargateError(f"Could not clean {len(failures)} of {len(run_ids)} runs.")
    return 0


def worktree_fingerprint(ctx: RunContext) -> str:
    """Digest the tracked and untracked state that an agent can change."""
    parts = [git_quiet(ctx.worktree, "diff", ctx.base_commit)]
    untracked = git_quiet(
        ctx.worktree, "ls-files", "--others", "--exclude-standard", "-z"
    )
    for name in untracked.split("\0"):
        if not name:
            continue
        # New files are implementation work too. Metadata errs toward allowing
        # a review rather than hashing an arbitrarily large untracked artifact.
        with contextlib.suppress(OSError):
            info = (ctx.worktree / name).lstat()
            parts.append(f"{name}\t{info.st_size}\t{info.st_mtime_ns}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def untracked_entries(worktree: Path) -> set[str]:
    """Return untracked paths at Git's directory-grouped granularity."""
    out = git_quiet(
        worktree,
        "ls-files",
        "--others",
        "--exclude-standard",
        "--directory",
        "--no-empty-directory",
        "-z",
    )
    return {name for name in out.split("\0") if name}


TEST_TAIL_LINES = 200


def plan_tests(ctx: RunContext) -> None:
    """Choose once from the original repository and explain the choice."""
    settings = ctx.config.get("settings", {})
    mode = detection_mode(ctx.config)
    configured = str(settings.get("test_command", "") or "").strip()
    ctx.test_command, ctx.detected = selected_test_command(ctx.config, ctx.repo)
    ctx.test_source = ""
    if configured:
        ctx.test_source = "settings.test_command"
        print(f"Tests:    {configured}   (settings.test_command)")
        return
    if mode == "off":
        ctx.test_source = "not configured; detection off"
        print("Tests:    none configured (test command detection is off)")
        return

    if not ctx.detected:
        ctx.test_source = "not configured; none detected"
        print("Tests:    none configured, none detected")
        return

    selected = ctx.detected[0]
    if mode == "auto":
        ctx.test_source = f"detected: {selected.source}"
        print(
            f"Tests:    {selected.command}   (detected: {selected.source}; "
            "automatic)"
        )
        for candidate in ctx.detected[1:]:
            print(
                f"          {candidate.command}   (detected: {candidate.source}; "
                "lower priority, not selected)"
            )
        return

    ctx.test_source = (
        f"detected {selected.command} from {selected.source}; report-only"
    )
    print(
        f"Tests:    {selected.command}   (detected: {selected.source}; not run -- "
        "settings.test_command_detection: report)"
    )
    for candidate in ctx.detected[1:]:
        print(
            f"          {candidate.command}   (detected: {candidate.source}; not run)"
        )
    print("          To confirm the first candidate, add to .stargate.yaml:")
    print("            settings:")
    print(f"              test_command: {selected.command!r}")


def run_tests(ctx: RunContext, label: str) -> tuple[int | None, str]:
    """Run the configured suite. Returns (exit code, report shown to agents)."""
    settings = ctx.config.get("settings", {})
    command = ctx.test_command
    if not command:
        print("\nNo automatic test_command configured; skipping orchestrator-level tests.")
        if ctx.detected:
            candidate = ctx.detected[0]
            return None, (
                "No test command is configured in the orchestrator. It detected "
                f"`{candidate.command}` ({candidate.source}) but did not run it. "
                "Run the project's own tests yourself if you can."
            )
        return None, (
            "No test command is configured in the orchestrator. "
            "Run the project's own tests yourself if you can."
        )

    kind = "detected" if ctx.test_source.startswith("detected:") else "configured"
    print(f"\nRunning {kind} test command: {command}")
    timeout = float(settings.get("test_timeout_seconds", 900)) or None
    before = untracked_entries(ctx.worktree)
    try:
        try:
            proc = subprocess.run(
                ["/bin/sh", "-lc", command],
                cwd=str(ctx.worktree),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
            )
            output, code = proc.stdout or "", proc.returncode
        except subprocess.TimeoutExpired as exc:
            output = (exc.output or "") + f"\n\n[timed out after {timeout}s]"
            code = 124
    finally:
        # Test suites commonly leave .venv/, *.egg-info/, target/ or
        # node_modules/. A tidy project ignores them, but an incomplete
        # .gitignore must not turn build output into history. Tracked rewrites
        # remain eligible because they are part of the diff the reviewer saw.
        # Persisting these names matters when a crash and resume span processes.
        ctx.test_artifacts |= untracked_entries(ctx.worktree) - before
        save_state(ctx, "running")

    print(output, end="" if output.endswith("\n") else "\n")
    (ctx.artifacts / f"tests-{label}.txt").write_text(
        f"$ {command}\n\n{output}\n\nexit_code={code}\n"
    )

    # Agents only need the tail; a full suite log blows the prompt budget.
    tail = "\n".join(output.splitlines()[-TEST_TAIL_LINES:])
    verdict = "PASSED" if code == 0 else f"FAILED (exit {code})"
    return code, f"$ {command}\n{verdict}\n\n{tail}"


COMMIT_SUBJECT_CHARS = 72
COMMIT_TIMEOUT_SECONDS = 600
COMMIT_OUTPUT_LINES = 20


def commit_message(ctx: RunContext, verdict: str, test_exit: int | None) -> str:
    task = " ".join(ctx.task.split()) or "run"
    suffix = f" ({verdict})"
    available = max(0, COMMIT_SUBJECT_CHARS - len("stargate: ") - len(suffix))
    subject_task = task[:available].rstrip()
    subject = f"stargate: {subject_task}{suffix}"

    if ctx.test_command:
        tests = (
            f"{ctx.test_command} (exit {test_exit})"
            if test_exit is not None else f"{ctx.test_command} (not run)"
        )
    elif "report-only" in ctx.test_source:
        tests = "not run (report-only detection)"
    else:
        tests = "not configured"

    trailers = [
        f"Stargate-Run-Id: {ctx.run_id}",
        f"Stargate-Verdict: {verdict}",
        f"Stargate-Base-Ref: {ctx.base_ref}",
        f"Stargate-Base-Commit: {ctx.base_commit}",
    ]
    if test_exit is not None:
        trailers.append(f"Stargate-Tests-Exit: {test_exit}")
    return f"""\
{subject}

Task: {task}
Verdict: {verdict}
Tests: {tests}
Base: {ctx.base_ref}
Base commit: {ctx.base_commit}

Produced by stargate agents, committed by the orchestrator; the agents
themselves never run git. Plan, review and traces:
.stargate/runs/{ctx.run_id}/

{chr(10).join(trailers)}
"""


def stage_run_changes(ctx: RunContext) -> bool:
    """Stage agent work while excluding untracked test-created entries."""
    excludes = [f":(exclude,literal){name}" for name in sorted(ctx.test_artifacts)]
    git(ctx.worktree, "add", "-A", "--", ".", *excludes, capture=True)
    proc = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=str(ctx.worktree),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode not in (0, 1):
        raise StargateError(
            f"git diff --cached --quiet failed in {ctx.worktree}: "
            f"{proc.stderr.strip()}"
        )
    return proc.returncode == 1


def commit_failure(ctx: RunContext, reason: str, output: str = "") -> None:
    tail = "\n".join(output.strip().splitlines()[-COMMIT_OUTPUT_LINES:])
    message = (
        f"Could not commit the run's work ({reason}). The changes are intact "
        f"in {ctx.worktree}; any successfully staged changes remain staged, "
        "and nothing was lost. A pre-commit hook or a commit signing "
        "configuration in this repository is the usual cause -- a hook that "
        "rewrote files may leave those rewrites unstaged. Finish by hand with:\n"
        f"  cd {shlex.quote(str(ctx.worktree))} && git commit"
    )
    if tail:
        message += "\nLast output:\n" + "\n".join(
            f"  {line}" for line in tail.splitlines()
        )
    ctx.commit_error = message
    print(f"\n{message}", file=sys.stderr)


def commit_run(ctx: RunContext, verdict: str, test_exit: int | None) -> str:
    """Make at most one commit after the run reaches a terminal verdict.

    Red results are committed too: they are the results most in need of a
    durable recovery point, and the private, unpushed branch plus verdict in
    the message keeps the history honest. Fixer passes are one editing session,
    so committing before the final review would create misleading checkpoints.
    """
    ctx.commit_error = ""
    if not ctx.worktree.exists():
        return "empty"

    branch = git_quiet(ctx.worktree, "rev-parse", "--abbrev-ref", "HEAD").strip()
    if branch != ctx.branch:
        commit_failure(
            ctx,
            f"worktree is on {branch!r}, not the run branch {ctx.branch!r}",
        )
        return "failed"

    try:
        changed = stage_run_changes(ctx)
    except StargateError as exc:
        commit_failure(ctx, f"staging failed: {exc}")
        return "failed"
    if not changed:
        return "empty"

    message_path = ctx.artifacts / "commit-message.txt"
    message_path.write_text(commit_message(ctx, verdict, test_exit))
    try:
        proc = run_process(
            ["git", "commit", "-F", str(message_path)],
            ctx.worktree,
            check=False,
            timeout=COMMIT_TIMEOUT_SECONDS,
        )
    except StargateError as exc:
        commit_failure(ctx, str(exc))
        return "failed"
    if proc.returncode:
        commit_failure(ctx, f"git exit {proc.returncode}", proc.stdout or "")
        return "failed"

    ctx.commit = git_quiet(ctx.worktree, "rev-parse", "HEAD").strip()
    # Known test output is deliberately left untracked, so it must not be
    # blamed on a hook. The same exclusions reveal only unexpected rewrites.
    excludes = [f":(exclude,literal){name}" for name in sorted(ctx.test_artifacts)]
    status = git_quiet(
        ctx.worktree, "status", "--porcelain", "--", ".", *excludes
    )
    if status:
        print(
            "\nWarning: the commit succeeded, but a repository hook modified "
            f"files afterward. Those changes remain uncommitted in {ctx.worktree}; "
            "stargate will not create a second commit.",
            file=sys.stderr,
        )
    return "committed"


def commit_summary(ctx: RunContext, enabled: bool) -> str:
    if not enabled:
        return "disabled"
    if ctx.commit_error:
        return "FAILED"
    if ctx.commit:
        return ctx.commit
    return "none (nothing to commit)"


def finish(
    ctx: RunContext,
    task: str,
    verdict: str,
    test_exit: int | None,
    *,
    commit: bool,
) -> int:
    if commit:
        commit_outcome = commit_run(ctx, verdict, test_exit)
    else:
        # A failed commit belongs to the invocation that attempted it. If the
        # user deliberately resumes without committing, preserving that error
        # would make a successful terminal result keep returning exit 5.
        ctx.commit_error = ""
        commit_outcome = "disabled"
    write_summary(ctx, task, verdict, test_exit, commit)
    save_state(ctx, verdict.lower())

    print("\n=== RESULT ===")
    print(f"Verdict:   {verdict}")
    print(f"Branch:    {ctx.branch}")
    print(f"Commit:    {commit_summary(ctx, commit)}")
    print(f"Worktree:  {ctx.worktree}")
    print(f"Artifacts: {ctx.artifacts}")
    if ctx.tokens_used:
        cap = token_cap(ctx.config)
        print(f"Tokens:    {ctx.tokens_used:,}" + (f" of {cap:,}" if cap else " (no cap)"))
    if ctx.commit:
        print("\nNothing was merged, pushed, or deleted automatically.")
    else:
        print("\nNothing was committed, merged, pushed, or deleted automatically.")
    print(f"Inspect with: cd {shlex.quote(str(ctx.worktree))} && git status && git diff {shlex.quote(ctx.base_commit)}")
    if ctx.commit:
        print(
            "History: git log --oneline "
            f"{shlex.quote(ctx.base_commit + '..' + ctx.branch)}"
        )
        print(
            "Build on it with: stargate run --base-ref "
            f"{shlex.quote(ctx.branch)} \"<next task>\""
        )

    if commit_outcome == "failed":
        return 5
    if verdict == "BUDGET_EXCEEDED":
        return 4
    if verdict != "APPROVED":
        return 2
    if test_exit not in (None, 0):
        return 3
    return 0


def write_summary(
    ctx: RunContext,
    task: str,
    verdict: str,
    test_exit: int | None,
    commit: bool,
) -> None:
    status = git(ctx.worktree, "status", "--short").stdout
    diff_stat = git(ctx.worktree, "diff", "--stat", ctx.base_commit).stdout
    summary = f"""# stargate run

Task: {task}
Run: {ctx.run_id}
Base ref: {ctx.base_ref}
Base commit: {ctx.base_commit}
Branch: {ctx.branch}
Commit: {commit_summary(ctx, commit)}
Worktree: {ctx.worktree}
Verdict: {verdict}
Test command: {ctx.test_command or "(none)"} ({ctx.test_source})
Test exit: {test_exit}
Tokens reported: {ctx.tokens_used:,}{" of " + format(token_cap(ctx.config), ",") if token_cap(ctx.config) else ""}

## git status

{status or "(clean)"}

## diff stat

{diff_stat or "(no tracked diff)"}
"""
    (ctx.artifacts / "summary.md").write_text(summary)


def orchestrate(args: argparse.Namespace, script_dir: Path, config: dict[str, Any]) -> int:
    repo = repo_root(Path.cwd())
    resuming = args.command == "resume"

    if resuming:
        ctx = load_run(repo, args.run_id, config, use_frozen=args.config is None)
        prompts = [ctx.artifacts / "prompts"]
        print(f"\nResuming {ctx.run_id}: {ctx.task}")
        if redo := set(args.redo):
            # Completion is the only reason a stage is skipped, so forgetting
            # that record replaces hand-editing state.json.
            ctx.done -= redo
            print(f"Redoing: {', '.join(sorted(redo))}")
            if "architect" in redo and "developer" in ctx.done:
                print(
                    "Warning: the developer is still marked complete, so the "
                    "new plan will not be implemented. Add --redo developer.",
                    file=sys.stderr,
                )
        print(f"Completed: {', '.join(sorted(ctx.done)) or '(nothing)'}")
    else:
        warn_if_dirty(repo)
        ctx = make_context(repo, config, args.task, args.base_ref, args.name)
        prompts = snapshot(ctx, prompt_dirs(config, script_dir))

    commit = commit_enabled(ctx.config) and not args.no_commit

    print(f"\nRun ID:   {ctx.run_id}")
    print(f"Base:     {ctx.base_ref} @ {ctx.base_commit[:12]}")
    print(f"Branch:   {ctx.branch}")
    print(f"Worktree: {ctx.worktree}")
    print(f"Artifacts:{ctx.artifacts}")

    try:
        plan_tests(ctx)
        return run_stages(ctx, args, prompts, commit=commit)
    except (StargateError, KeyboardInterrupt) as exc:
        save_state(ctx, "failed", f"{type(exc).__name__}: {exc}")
        print(f"\nResume with: stargate resume {ctx.run_id}", file=sys.stderr)
        raise


def run_stages(
    ctx: RunContext,
    args: argparse.Namespace,
    prompts: list[Path],
    *,
    commit: bool,
) -> int:
    config = ctx.config
    plan_path = ctx.artifacts / "plan.md"
    architect_ran = False

    # 1. Architect reads the original repository and emits a plan.
    if "architect" in ctx.done:
        raw_plan = plan_path.read_text().strip()
        print(f"\n=== ARCHITECT (skipped, reusing {plan_path}) ===")
    else:
        architect_ran = True
        enter_stage(ctx, "architect")
        architect_prompt = render_prompt(
            prompts, "architect", task=ctx.task, base_ref=ctx.base_ref
        )
        print("\n=== ARCHITECT ===")
        raw_plan = invoke_agent(
            ctx, "architect", architect_prompt, ctx.repo, plan_path
        ).strip()

    architect_name, plan = split_plan_name(raw_plan)
    if not plan:
        raise StargateError("Architect returned an empty plan.")
    # The worktree is deliberately created after planning, so the branch can
    # still adopt the architect's vocabulary. The run id was already printed
    # and names artifacts/worktree paths; changing it here would break resume.
    # An existing worktree is stronger evidence than a missing completion bit
    # after a crash, and must never be orphaned by a rename.
    if (
        architect_name and not ctx.named_by_user and ctx.tag
        and "worktree" not in ctx.done and not ctx.worktree.exists()
    ):
        proposed = f"stargate/{architect_name}-{ctx.tag}"
        ctx.branch = unique_branch(ctx.repo, proposed)
        save_state(ctx, "running")
        print(f"Branch:   {ctx.branch}   (named by the architect)")
    if architect_ran:
        complete_stage(ctx, "architect")

    # 2. Create isolated implementation branch/worktree.
    enter_stage(ctx, "worktree")
    print("\n=== WORKTREE ===")
    create_worktree(ctx)
    complete_stage(ctx, "worktree")

    if budget_spent(ctx, "the developer"):
        return finish(ctx, ctx.task, "BUDGET_EXCEEDED", None, commit=commit)

    # 3. Developer implements.
    if "developer" in ctx.done:
        print("\n=== DEVELOPER (skipped, already ran in this run) ===")
    else:
        enter_stage(ctx, "developer")
        before = worktree_fingerprint(ctx)
        developer_prompt = render_prompt(
            prompts,
            "developer",
            task=ctx.task,
            base_ref=ctx.base_ref,
            plan=plan,
        )
        print("\n=== DEVELOPER ===")
        invoke_agent(
            ctx,
            "developer",
            developer_prompt,
            ctx.worktree,
            ctx.artifacts / "developer.txt",
        )
        if worktree_fingerprint(ctx) == before:
            # Leaving this incomplete is intentional: a reviewer can only
            # rediscover the empty diff, while resume must rerun this stage.
            raise StargateError(
                f"The developer changed nothing in {ctx.worktree}; there is "
                f"nothing to review. Check the trace in "
                f"{ctx.artifacts / 'developer.txt.log'}, verify the agent's "
                "tools with 'stargate doctor --probe', then resume: this stage "
                "was not recorded as complete."
            )
        complete_stage(ctx, "developer")

    test_exit, test_report = run_tests(ctx, "developer")

    # 4. Review/fix loop.
    configured_loops = int(config.get("settings", {}).get("max_review_loops", 2))
    max_loops = args.max_review_loops if args.max_review_loops is not None else configured_loops
    verdict = "CHANGES_REQUESTED"

    # ponytail: resume always re-runs the review loop from the first attempt.
    # Re-reviewing is idempotent and cheap next to re-implementing; per-attempt
    # resume would need every fixer pass recorded separately.
    enter_stage(ctx, "review")
    for attempt in range(max_loops + 1):
        if budget_spent(ctx, f"review {attempt + 1}"):
            return finish(
                ctx, ctx.task, "BUDGET_EXCEEDED", test_exit, commit=commit
            )
        review_prompt = render_prompt(
            prompts,
            "reviewer",
            task=ctx.task,
            base_ref=ctx.base_ref,
            plan=plan,
            tests=test_report,
        )
        print(f"\n=== REVIEW {attempt + 1} ===")
        review = invoke_agent(
            ctx,
            "reviewer",
            review_prompt,
            ctx.worktree,
            ctx.artifacts / f"review-{attempt + 1}.md",
        ).strip()

        # The verdict is the last line, not a substring anywhere in the prose:
        # "I cannot give VERDICT: APPROVED because..." must not read as approval.
        last_line = review.splitlines()[-1].strip() if review else ""
        if last_line == "VERDICT: APPROVED":
            verdict = "APPROVED"
            break

        if last_line != "VERDICT: CHANGES_REQUESTED":
            raise StargateError(
                "Reviewer did not end its response with a recognized verdict. "
                f"See {ctx.artifacts / f'review-{attempt + 1}.md'}"
            )

        if attempt >= max_loops:
            verdict = "CHANGES_REQUESTED"
            break

        fixer_prompt = render_prompt(
            prompts,
            "fixer",
            task=ctx.task,
            base_ref=ctx.base_ref,
            plan=plan,
            review=review,
            tests=test_report,
        )
        if budget_spent(ctx, f"fixer {attempt + 1}"):
            return finish(
                ctx, ctx.task, "BUDGET_EXCEEDED", test_exit, commit=commit
            )

        before = worktree_fingerprint(ctx)
        print(f"\n=== FIXER {attempt + 1} ===")
        invoke_agent(
            ctx,
            "fixer",
            fixer_prompt,
            ctx.worktree,
            ctx.artifacts / f"fix-{attempt + 1}.txt",
        )
        if worktree_fingerprint(ctx) == before:
            # A fixer may reasonably reject a review finding, but another
            # review of the identical tree would only repeat the same verdict.
            print(
                f"\n[fixer {attempt + 1}] changed nothing; a further review "
                "would reach the same verdict. Stopping the review loop.",
                file=sys.stderr,
            )
            break
        test_exit, test_report = run_tests(ctx, f"fix-{attempt + 1}")

    complete_stage(ctx, "review")
    return finish(ctx, ctx.task, verdict, test_exit, commit=commit)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stargate",
        description="Tiny Claude Code + Codex CLI multi-agent orchestrator.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help=f"Complete standalone config. Without it, ./{PROJECT_CONFIG}, "
        "the user config and packaged defaults are layered.",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    doctor_parser = sub.add_parser(
        "doctor", help="Check local CLI dependencies and configuration."
    )
    doctor_parser.add_argument(
        "--probe", action="store_true",
        help="Make one real, potentially billable call to each unique agent.",
    )
    sub.add_parser(
        "init-config",
        help="Copy the packaged agents.yaml to ~/.config/stargate/agents.yaml.",
    )
    sub.add_parser(
        "init-prompts",
        help="Copy the packaged prompts to ~/.config/stargate/prompts/ so they "
        "can be edited without touching the install.",
    )
    sub.add_parser(
        "list", aliases=["runs"],
        help="List the runs recorded in this repository, newest first."
    )
    clean = sub.add_parser(
        "clean", help="Remove a run's merged branch, clean worktree and artifacts."
    )
    clean.add_argument("run_id", nargs="?", help="Run ID shown by 'stargate list'.")
    clean.add_argument(
        "--all", action="store_true", dest="all_runs",
        help="Clean every recorded run that passes the safety checks.",
    )

    run = sub.add_parser("run", help="Plan, implement, review and fix a task.")
    run.add_argument("task", help="Feature/bug/task description.")
    run.add_argument(
        "--base-ref",
        default=None,
        help="Git ref to branch from. Defaults to the current branch/ref.",
    )
    run.add_argument(
        "--name",
        default=None,
        help="Short name for the branch and run id, e.g. --name 'passkey auth'. "
        "Overrides the name the architect suggests.",
    )

    resume = sub.add_parser(
        "resume",
        help="Continue a run that failed partway, reusing its plan, worktree, "
        "config and prompts.",
    )
    resume.add_argument("run_id", help="Run ID, as printed by the original run.")
    resume.add_argument(
        "--redo", action="append", default=[], choices=REDOABLE_STAGES,
        metavar="STAGE",
        help="Run this completed stage again instead of skipping it "
        f"({', '.join(REDOABLE_STAGES)}). Repeatable.",
    )

    for parser_ in (run, resume):
        parser_.add_argument(
            "--no-commit",
            action="store_true",
            help="Leave the run's work uncommitted in the worktree "
            "(overrides settings.commit).",
        )
        parser_.add_argument(
            "--max-review-loops",
            type=int,
            default=None,
            help="Override settings.max_review_loops.",
        )
    return parser


def install_signal_handlers() -> None:
    """Turn catchable termination into the existing resumable failure path."""
    handled = [
        signum for name in ("SIGTERM", "SIGHUP")
        if (signum := getattr(signal, name, None)) is not None
    ]

    def terminate(signum: int, _frame: Any) -> None:
        # One shot lets a second signal terminate even if cleanup gets stuck.
        for handled_signum in handled:
            signal.signal(handled_signum, signal.SIG_DFL)
        raise Terminated(signum)

    for signum in handled:
        signal.signal(signum, terminate)


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "init-config":
        return init_config(script_dir)
    if args.command == "init-prompts":
        return init_prompts(script_dir)

    try:
        if args.command in ("run", "resume"):
            # SIGINT already becomes KeyboardInterrupt; replacing it would add
            # a second path for an interrupt that is already recorded safely.
            install_signal_handlers()

        if args.command in ("list", "runs"):
            return list_runs(repo_root(Path.cwd()))
        if args.command == "clean":
            return clean_runs(repo_root(Path.cwd()), args.run_id, args.all_runs)

        config_paths = resolve_config(args.config, script_dir)
        config, layers = load_config(config_paths)
        if args.command == "doctor":
            return doctor(
                config,
                layers,
                script_dir,
                probe=args.probe,
                explicit_config=args.config is not None,
            )
        if args.command in ("run", "resume"):
            return orchestrate(args, script_dir, config)
        parser.error("Unknown command")
        return 2
    except StargateError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt as exc:
        signum = getattr(exc, "signum", signal.SIGINT)
        message = "Interrupted" if signum == signal.SIGINT else "Terminated"
        print(f"\n{message}.", file=sys.stderr)
        return 128 + int(signum)
