"""`stargate doctor`: report the effective configuration and, on request,
probe each distinct agent for the capabilities its role needs."""
from __future__ import annotations

import os
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
    PROMPTS,
    ROLES,
    TEST_COMMAND_PLACEHOLDER,
    agent_command,
    agent_entry,
    agent_env,
    blocking_severities,
    commit_enabled,
    env_summary,
    expand_test_command,
    find_prompt,
    prompt_dirs,
    pull_request_command,
    task_sources,
    test_command_grant,
    token_cap,
    value_source,
)
from .core import StargateError, run_process
from .detect import detection_mode, selected_test_command

PROBE_TIMEOUT_DEFAULT = 120

# Non-agent requirements keep resolving in stargate's environment. Task source
# and pull-request env handling is a separate follow-up to the agent checks.
ORCHESTRATOR = "stargate"


PROBE_CAPABILITIES = ("read", "write")


# Coding-agent CLIs stargate knows how to talk about. It configures none of
# them: a command prefix needs vendor-specific flags (read-only for the
# architect, where the final message goes, sandbox for the writers), and
# guessing those would produce a config that runs and does the wrong thing.
# Naming what is installed is the part that can be done without guessing --
# `examples/` carries the four verified ones.
KNOWN_AGENT_CLIS = {
    "claude": "Claude Code (examples/claude)",
    "codex": "OpenAI Codex CLI (examples/codex)",
    "kiro-cli": "Kiro CLI (examples/kiro)",
    "amp": "Sourcegraph Amp",
    "copilot": "GitHub Copilot CLI",
    "crush": "Charm Crush",
    "cursor-agent": "Cursor CLI",
    "gemini": "Gemini CLI (examples/gemini)",
    "goose": "Block Goose",
    "opencode": "opencode",
    "q": "Amazon Q Developer CLI",
    "qwen": "Qwen Code",
}


# The wrappers this repository ships under `examples/`. A config whose command
# prefix is one of them drives the vendor underneath it, so the vendor is in
# use even though its executable never appears in the config. A wrapper the
# user wrote under a name of their own cannot be seen from here.
AGENT_CLI_WRAPPERS = {
    "claude-json-stargate": "claude",
    "kiro-stargate": "kiro-cli",
}

# Kiro resolves ${KIRO_BIN:-/Applications/Kiro CLI.app/Contents/MacOS/kiro-cli}
# itself: the app-bundle path avoids the Homebrew symlink breaking its sibling
# executable lookup. Requiring kiro-cli on PATH would reject working installs.
# The other wrappers invoke their CLI bare, so PATH is required by default;
# add an exception here only when a wrapper carries its own CLI lookup.
WRAPPERS_WITH_THEIR_OWN_CLI_LOOKUP = {"kiro-stargate"}


def available_agent_clis(configured: set[str]) -> list[tuple[str, str, str]]:
    """Known agent CLIs on PATH that this config does not use: (name, path, what).

    A command prefix may name its executable by path (`/usr/local/bin/gemini`),
    so the comparison is on basenames: reporting a configured agent as an
    unused alternative reads as advice to change what already works. For the
    same reason a shipped wrapper counts as the vendor it calls.

    Only a configured executable that resolves counts as in use. A stale
    absolute path is reported `MISSING`, and the working CLI of the same name
    on PATH is the answer to it -- suppressing that leaves the reader with the
    dead end this report exists to end.
    """
    in_use = {Path(binary).name for binary in configured if shutil.which(binary)}
    in_use |= {AGENT_CLI_WRAPPERS[name] for name in in_use & AGENT_CLI_WRAPPERS.keys()}
    found = []
    for name, description in sorted(KNOWN_AGENT_CLIS.items()):
        if name in in_use:
            continue
        if path := shutil.which(name):
            found.append((name, path, description))
    return found


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
    # Only the runner's log_path branch kills the whole process group on timeout:
    # a direct-child kill can leave a spawned agent running and billing.
    try:
        proc = run_process(
            [*cmd, prompt], cwd, check=False, timeout=timeout,
            log_path=output.with_suffix(".log"), env=env,
            timeout_is_error=False, display_args=cmd,
        )
    except (StargateError, OSError) as exc:
        return str(exc)

    # The shared runner uses 124 for timeouts, and an agent that genuinely exits
    # 124 is indistinguishable from one -- some wrappers propagate a downstream
    # timeout(1) status. Keep the captured output either way, so a real failure
    # still explains itself instead of being reported only as a timeout that may
    # never have happened.
    if proc.returncode == 124:
        timed_out = f"probe timed out after {timeout}s"
        tail = (proc.stdout or "").strip()
        return f"{timed_out}; last output: {tail}" if tail else timed_out
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


def agent_search_path(env: dict[str, str] | None) -> str | None:
    """Match subprocess lookup, including the default search when PATH is removed."""
    # Passing None to which would reuse doctor's PATH even when the child has
    # explicitly removed it. get_exec_path supplies the child's exec default.
    return None if env is None else os.pathsep.join(os.get_exec_path(env))


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
    required: dict[str, dict[str, str | None]] = {}

    def require(binary: str, by: str, search_path: str | None) -> None:
        required.setdefault(binary, {})[by] = search_path

    require("git", ORCHESTRATOR, None)
    try:
        sources = task_sources(config)
        pr_command = pull_request_command(config)
        search_paths = {
            role: agent_search_path(agent_env(agent_entry(config, role)))
            for role in ROLES
        }
    except StargateError as exc:
        # Returns instead of setting ok=False like blocking_severities does, and
        # the difference is position: this runs before the probe block, so
        # continuing would pay for real agent calls under a config `run` refuses.
        print(f"\nERROR    {exc}")
        return 1
    if pr_command:
        require(pr_command[0], ORCHESTRATOR, None)
    for source in sources:
        for command in source["commands"]:
            require(command[0], ORCHESTRATOR, None)
    direct_heads = {commands[role][0] for role in ROLES}
    required_by: dict[str, str] = {}
    for role in ROLES:
        head = commands[role][0]
        require(head, role, search_paths[role])
        wrapper = Path(head).name
        cli = AGENT_CLI_WRAPPERS.get(wrapper)
        if cli is not None and wrapper not in WRAPPERS_WITH_THEIR_OWN_CLI_LOOKUP:
            # The wrapper invokes its CLI inside the same agent environment.
            require(cli, role, search_paths[role])
            required_by.setdefault(cli, wrapper)

    # FOUND requires every requirer's environment to resolve the binary: one
    # working role must not hide another that cannot run. Name failing roles
    # when environments differ so a CLI on doctor's PATH is not a mystery MISSING.
    for binary in sorted(required):
        requirers = required[binary]
        resolved = {
            who: shutil.which(binary, path=search)
            for who, search in requirers.items()
        }
        missing = [who for who, path in resolved.items() if not path]
        state = "MISSING" if missing else "FOUND"
        # Environments may resolve to different files; show the first requirer's
        # path, while the notes explain any failure that changes the exit code.
        path = "" if missing else (next(iter(resolved.values())) or "")
        notes = []
        if binary in required_by:
            notes.append(
                f"configured directly; also required by {required_by[binary]}"
                if binary in direct_heads else f"required by {required_by[binary]}"
            )
        if missing and (
            len(missing) != len(resolved)
            or any(requirers[who] is not None for who in missing)
        ):
            notes.append("not on the PATH of: " + ", ".join(missing))
        note = f"  -- {'; '.join(notes)}" if notes else ""
        print(f"{state:8} {binary:12} {path}{note}")
        ok = ok and not missing
    print(
        "\nFOUND means the executable is on PATH for every agent that needs it.\n"
        "An agent entry's `env:` can change or remove PATH, so a line can be\n"
        "MISSING for one role while the same name resolves for stargate itself.\n"
        "Authentication, credits, quota and model availability are NOT checked --\n"
        "an agent can still fail on its first call (e.g. \"Credit balance is too low\")."
    )

    if others := available_agent_clis(set(required)):
        print("\nOther agent CLIs on PATH, not used by this config:")
        for name, path, description in others:
            print(f"         {name:12} {path}  -- {description}")
        print(
            "         Stargate drives any of them as a command prefix, but the flags\n"
            "         differ per vendor; write the agent entry yourself, starting from\n"
            "         examples/README.md, and verify it with `stargate doctor --probe`."
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

    try:
        blocking_severities(config)
    except StargateError as exc:
        # Reporting a value that `run` will refuse would make doctor the wrong
        # place to find out, which is the one thing it exists for.
        print(f"\nERROR    {exc}")
        ok = False

    print("\nEffective settings:")
    for key, default in (
        ("max_review_loops", 2),
        ("blocking_severities", []),
        ("max_fanout_tasks", 8),
        ("max_parallel_tasks", 2),
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

    print("\nTask sources:")
    if not sources:
        print("  (none configured; 'stargate run --from' needs one)")
    for source in sources:
        print(f"  {', '.join(source['hosts'])}")
        for command in source["commands"]:
            print(f"    $ {shlex.join(command)}")
        if overrides := env_summary(source):
            print(f"    └─ env: {overrides}")

    if pr_command:
        print("\nPull request:")
        print(f"  {shlex.join(pr_command)}")
        if overrides := env_summary(config["pull_request"]):
            print(f"    └─ env: {overrides}")
        print("  Published only when you pass --pr; configuration alone never publishes.")

    print("\nPrompts:")
    dirs = prompt_dirs(config, script_dir)
    for role in PROMPTS:
        try:
            print(f"  {role:10} {find_prompt(dirs, role)}")
        except StargateError as exc:
            print(f"  {role:10} MISSING ({exc})")
            ok = False

    return 0 if ok else 1
