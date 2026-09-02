"""`stargate doctor`: report the effective configuration and, on request,
probe each distinct agent for the capabilities its role needs."""
from __future__ import annotations

import shlex
import shutil
import subprocess
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
    ROLES,
    TEST_COMMAND_PLACEHOLDER,
    agent_command,
    agent_entry,
    agent_env,
    commit_enabled,
    env_summary,
    expand_test_command,
    find_prompt,
    prompt_dirs,
    test_command_grant,
    token_cap,
    value_source,
)
from .core import StargateError
from .detect import detection_mode, selected_test_command

PROBE_TIMEOUT_DEFAULT = 120


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
            tuple(sorted((str(k), v) for k, v in declared.items()))
            if isinstance(declared, dict) else None,
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
