#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import shlex
import shutil
import json
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class StargateError(RuntimeError):
    pass


@dataclass
class RunContext:
    repo: Path
    config: dict[str, Any]
    run_id: str
    slug: str
    branch: str
    base_ref: str
    worktree: Path
    artifacts: Path
    task: str = ""
    stage: str = "init"
    done: set[str] = field(default_factory=set)
    tokens_used: int = 0


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
                print(f"  ... {elapsed:.0f}s elapsed, {size:,} bytes written", flush=True)
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


# Per-project override. Deliberately NOT "agents.yaml": that name is common
# enough that a global install would silently pick up an unrelated repo's file.
PROJECT_CONFIG = ".stargate.yaml"

ROLES = ("architect", "developer", "reviewer", "fixer")


def user_config() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(os.path.expanduser(base)) / "stargate" / "agents.yaml"


def resolve_config(arg: str | None, script_dir: Path) -> Path:
    if arg:
        return Path(arg).expanduser().resolve()
    for candidate in ((Path.cwd() / PROJECT_CONFIG).resolve(), user_config()):
        if candidate.exists():
            return candidate
    return script_dir / "agents.yaml"  # packaged defaults


def init_prompts(script_dir: Path) -> int:
    target = user_config().parent / "prompts"
    target.mkdir(parents=True, exist_ok=True)
    for name in ROLES:
        dest = target / f"{name}.md"
        if dest.exists():
            print(f"kept    {dest}")
            continue
        dest.write_text((script_dir / "prompts" / f"{name}.md").read_text())
        print(f"wrote   {dest}")
    print("\nEdit these to override the defaults. Delete one to fall back.")
    return 0


def init_config(script_dir: Path) -> int:
    target = user_config()
    if target.exists():
        print(f"Already exists, not overwriting: {target}")
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text((script_dir / "agents.yaml").read_text())
    print(f"Wrote {target}\nEdit it to set test_command, models, timeouts.")
    return 0


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise StargateError(f"Config not found: {path}")
    data = yaml.safe_load(path.read_text()) or {}
    if "agents" not in data or "workflow" not in data:
        raise StargateError("Config must contain 'agents' and 'workflow'.")
    return data


def slugify(text: str, max_len: int = 42) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return (text or "task")[:max_len].rstrip("-")


def resolve_base_ref(repo: Path, requested: str | None) -> str:
    if requested:
        git(repo, "rev-parse", "--verify", requested, capture=True)
        return requested
    proc = git(repo, "rev-parse", "--abbrev-ref", "HEAD", capture=True)
    branch = proc.stdout.strip()
    if branch == "HEAD":
        return git(repo, "rev-parse", "HEAD").stdout.strip()
    return branch


def warn_if_dirty(repo: Path) -> None:
    status = git(repo, "status", "--porcelain").stdout.strip()
    if status:
        print(
            "\nWarning: the source repository has local changes. "
            "The agent worktree is created from the selected base ref, "
            "so those uncommitted source-tree changes are NOT copied.",
            file=sys.stderr,
        )


def agent_entry(config: dict[str, Any], role: str) -> dict[str, Any]:
    try:
        return config["agents"][config["workflow"][role]]
    except KeyError as exc:
        raise StargateError(f"Invalid agent configuration for role '{role}'.") from exc


def agent_env(entry: dict[str, Any]) -> dict[str, str] | None:
    """The environment for one agent, or None to inherit unchanged.

    A null value REMOVES the variable. That is the case worth supporting: an
    ANTHROPIC_API_KEY exported globally shadows the CLI's own login, and
    without this the only fix is to unset it for the whole orchestrator.
    """
    declared = entry.get("env")
    if not declared:
        return None
    if not isinstance(declared, dict):
        raise StargateError("An agent's 'env' must be a mapping of names to values.")
    env = dict(os.environ)
    for key, value in declared.items():
        if value is None:
            env.pop(str(key), None)
        else:
            env[str(key)] = str(value)
    return env


def env_summary(entry: dict[str, Any]) -> str:
    """Which variables an agent overrides. Names only -- values are secrets."""
    declared = entry.get("env") or {}
    if not isinstance(declared, dict) or not declared:
        return ""
    return ", ".join(
        f"{key} (unset)" if value is None else str(key)
        for key, value in declared.items()
    )


def parse_usage(transcript: str, pattern: str | None) -> int:
    """Tokens an agent reported spending, via a regex the CONFIG supplies.

    The orchestrator cannot see inside an agent — most of a run's tokens are the
    model reading the repo, never crossing this process. So the only usable
    number is whatever the CLI prints, and the shape of that is the vendor's
    business, not this file's.
    """
    if not pattern:
        return 0
    match = re.search(pattern, transcript)
    if not match or not match.groups():
        return 0
    try:
        return int(match.group(1).replace(",", "").replace(".", "").replace("_", ""))
    except ValueError:
        return 0


def token_cap(config: dict[str, Any]) -> int:
    return int(config.get("settings", {}).get("max_task_tokens", 0) or 0)


def agent_command(config: dict[str, Any], role: str) -> list[str]:
    command = agent_entry(config, role).get("command")
    if not isinstance(command, list) or not command:
        raise StargateError(f"Agent for role '{role}' needs a non-empty command list.")
    return [str(x) for x in command]


PROBE_TIMEOUT_DEFAULT = 120

# How often a running agent prints that it is still alive.
HEARTBEAT_SECONDS = 30


def unique_agents(config: dict[str, Any]) -> dict[Any, tuple[list[str], Any, dict[str, Any]]]:
    """Distinct agent invocations: the four default roles map onto two commands,
    and probing per role would bill twice for nothing.

    Identity is command AND environment. Two roles running the same command
    under different credentials are two different things to verify -- deduping
    on the command alone would report one of them without ever calling it.
    """
    agents: dict[Any, tuple[list[str], Any, dict[str, Any]]] = {}
    for role in ROLES:
        entry = agent_entry(config, role)
        declared = entry.get("env") or {}
        key = (
            tuple(agent_command(config, role)),
            tuple(sorted((str(k), v) for k, v in declared.items())) if isinstance(declared, dict) else None,
        )
        names, known, first = agents.get(key, ([], None, entry))
        names.append(config["workflow"][role])
        agents[key] = (names, known if known is not None else entry.get("probe"), first)
    return agents


def probe_one(command: tuple[str, ...], prompt: str, cwd: Path, output: Path,
              timeout: float | None, env: dict[str, str] | None) -> str:
    """Empty string on success, otherwise the reason it failed."""
    cmd = [part.replace("{output}", str(output)) for part in command]
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
    if any("{output}" in part for part in command):
        if not (output.read_text() if output.exists() else "").strip():
            return ("agent declares {output} but wrote nothing; check that its "
                    "CLI supports the configured flag")
    return ""


def probe_agents(config: dict[str, Any]) -> bool:
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

        for index, (key, (names, prompt, entry)) in enumerate(unique_agents(config).items()):
            command = key[0]
            label = ", ".join(dict.fromkeys(names))
            if overrides := env_summary(entry):
                label += f" (env: {overrides})"
            if prompt is None:
                print(f"  SKIP {label} (no probe configured)")
                continue
            if not isinstance(prompt, str) or not prompt.strip():
                print(f"  FAIL {label} (probe must be a non-empty string)")
                ok = False
                continue
            started = time.monotonic()
            error = probe_one(
                command, prompt, cwd, cwd / f"output-{index}.txt", timeout,
                agent_env(entry),
            )
            print(f"  {'FAIL' if error else 'OK':4} {label} [{time.monotonic() - started:.1f}s]")
            if error:
                print("       " + error.replace("\n", "\n       "))
                ok = False
    return ok


def doctor(
    config: dict[str, Any], config_path: Path, script_dir: Path, *, probe: bool = False
) -> int:
    print("stargate doctor\n")
    print(f"Config:  {config_path}\n")
    ok = True
    binaries = {"git"}
    for role in ROLES:
        cmd = agent_command(config, role)
        binaries.add(cmd[0])

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
        ok = probe_agents(config) and ok

    packaged = yaml.safe_load((script_dir / "agents.yaml").read_text()) or {}
    mine, theirs = config.get("version"), packaged.get("version")
    if mine is not None and theirs is not None and mine != theirs:
        print(
            f"\nWARN     config version {mine} differs from the packaged version "
            f"{theirs}.\n         Newer defaults may be missing; compare against "
            f"{script_dir / 'agents.yaml'}."
        )

    settings = config.get("settings", {})
    print("\nEffective settings:")
    for key, default in (
        ("max_review_loops", 2),
        ("test_command", ""),
        ("max_task_tokens", 0),
        ("agent_timeout_seconds", 1800),
        ("test_timeout_seconds", 900),
        ("worktree_root", ""),
        ("prompts_dir", ""),
    ):
        value = settings.get(key, default)
        print(f"  {key:22} {value!r}" + ("" if key in settings else "   (default)"))

    cap = token_cap(config)
    print("\nAgents:")
    for role in ROLES:
        entry = agent_entry(config, role)
        print(f"  {role:10} {shlex.join(agent_command(config, role))}")
        if overrides := env_summary(entry):
            print(f"  {'':10} └─ env: {overrides}")
        if cap:
            meters = "reports usage" if entry.get("usage_pattern") else "no usage_pattern"
            print(f"  {'':10} └─ {meters}")

    print("\nPrompts:")
    dirs = prompt_dirs(config, script_dir)
    for role in ROLES:
        try:
            print(f"  {role:10} {find_prompt(dirs, role)}")
        except StargateError as exc:
            print(f"  {role:10} MISSING ({exc})")
            ok = False

    return 0 if ok else 1


def prompt_dirs(config: dict[str, Any], script_dir: Path) -> list[Path]:
    """Prompt sources, most specific first. Overrides are per-file: a custom
    reviewer.md is picked up while the other three fall back to the defaults."""
    configured = str(config.get("settings", {}).get("prompts_dir", "") or "").strip()
    dirs = [Path(os.path.expanduser(configured)).resolve()] if configured else []
    return [*dirs, user_config().parent / "prompts", script_dir / "prompts"]


def find_prompt(dirs: list[Path], name: str) -> Path:
    for base in dirs:
        candidate = base / f"{name}.md"
        if candidate.exists():
            return candidate
    searched = ", ".join(str(d) for d in dirs)
    raise StargateError(f"Prompt {name}.md not found in: {searched}")


def render_prompt(dirs: list[Path], name: str, **values: str) -> str:
    """Substitute only the placeholders we define, by literal replacement.

    Not str.format: a custom prompt is free to contain JSON, CSS or an f-string
    example, and every brace in it would otherwise have to be escaped or the
    run dies with KeyError before a single agent starts.
    """
    text = find_prompt(dirs, name).read_text()
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text


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
    """
    cmd = agent_command(ctx.config, role)
    writes_final = any("{output}" in part for part in cmd)
    cmd = [part.replace("{output}", str(output_path)) for part in cmd]
    log_path = output_path.with_name(output_path.name + ".log")

    print(f"trace: tail -f {shlex.quote(str(log_path))}", flush=True)
    started = time.monotonic()
    timeout = float(ctx.config.get("settings", {}).get("agent_timeout_seconds", 1800))
    proc = run_process(
        [*cmd, prompt], cwd, timeout=timeout or None, log_path=log_path,
        env=agent_env(agent_entry(ctx.config, role)),
    )
    transcript = proc.stdout or ""
    print(f"\n[{role}] exit {proc.returncode} in {time.monotonic() - started:.0f}s", flush=True)

    used = parse_usage(transcript, agent_entry(ctx.config, role).get("usage_pattern"))
    ctx.tokens_used += used
    if used:
        cap = token_cap(ctx.config)
        budget = f" of {cap:,}" if cap else ""
        print(f"\n[{role}] reported {used:,} tokens; {ctx.tokens_used:,}{budget} used so far.")

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


def make_context(
    repo: Path,
    config: dict[str, Any],
    task: str,
    base_ref: str | None,
) -> RunContext:
    now = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = slugify(task)
    run_id = f"{now}-{slug}"
    base = resolve_base_ref(repo, base_ref)
    branch = f"stargate/{slug}-{now}"

    configured = str(config.get("settings", {}).get("worktree_root", "") or "").strip()
    worktree_root = Path(os.path.expanduser(configured)).resolve() if configured else default_worktree_root(repo)
    worktree = worktree_root / run_id
    artifacts = repo / ".stargate" / "runs" / run_id
    artifacts.mkdir(parents=True, exist_ok=False)
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
        worktree=worktree,
        artifacts=artifacts,
        task=task,
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
        "branch": ctx.branch,
        "worktree": str(ctx.worktree),
        "stage": ctx.stage,
        "status": status,
        "error": error,
        "completed": sorted(ctx.done),
        "tokens_used": ctx.tokens_used,
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
        worktree=Path(state["worktree"]),
        artifacts=artifacts,
        task=state["task"],
        stage=state.get("stage", "init"),
        done=set(state.get("completed", [])),
        tokens_used=int(state.get("tokens_used", 0)),
    )


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
        else ["-b", ctx.branch, str(ctx.worktree), ctx.base_ref]
    )
    git(ctx.repo, *args, capture=True)


TEST_TAIL_LINES = 200


def run_tests(ctx: RunContext, label: str) -> tuple[int | None, str]:
    """Run the configured suite. Returns (exit code, report shown to agents)."""
    settings = ctx.config.get("settings", {})
    command = str(settings.get("test_command", "") or "").strip()
    if not command:
        print("\nNo automatic test_command configured; skipping orchestrator-level tests.")
        return None, (
            "No test command is configured in the orchestrator. "
            "Run the project's own tests yourself if you can."
        )

    print(f"\nRunning configured test command: {command}")
    timeout = float(settings.get("test_timeout_seconds", 900)) or None
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

    print(output, end="" if output.endswith("\n") else "\n")
    (ctx.artifacts / f"tests-{label}.txt").write_text(
        f"$ {command}\n\n{output}\n\nexit_code={code}\n"
    )

    # Agents only need the tail; a full suite log blows the prompt budget.
    tail = "\n".join(output.splitlines()[-TEST_TAIL_LINES:])
    verdict = "PASSED" if code == 0 else f"FAILED (exit {code})"
    return code, f"$ {command}\n{verdict}\n\n{tail}"


def finish(ctx: RunContext, task: str, verdict: str, test_exit: int | None) -> int:
    write_summary(ctx, task, verdict, test_exit)
    save_state(ctx, verdict.lower())

    print("\n=== RESULT ===")
    print(f"Verdict:   {verdict}")
    print(f"Branch:    {ctx.branch}")
    print(f"Worktree:  {ctx.worktree}")
    print(f"Artifacts: {ctx.artifacts}")
    if ctx.tokens_used:
        cap = token_cap(ctx.config)
        print(f"Tokens:    {ctx.tokens_used:,}" + (f" of {cap:,}" if cap else " (no cap)"))
    print("\nNothing was committed, merged, pushed, or deleted automatically.")
    print(f"Inspect with: cd {shlex.quote(str(ctx.worktree))} && git status && git diff {shlex.quote(ctx.base_ref)}")

    if verdict == "BUDGET_EXCEEDED":
        return 4
    if verdict != "APPROVED":
        return 2
    if test_exit not in (None, 0):
        return 3
    return 0


def write_summary(ctx: RunContext, task: str, verdict: str, test_exit: int | None) -> None:
    status = git(ctx.worktree, "status", "--short").stdout
    diff_stat = git(ctx.worktree, "diff", "--stat", ctx.base_ref).stdout
    summary = f"""# stargate run

Task: {task}
Run: {ctx.run_id}
Base ref: {ctx.base_ref}
Branch: {ctx.branch}
Worktree: {ctx.worktree}
Verdict: {verdict}
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
        print(f"Completed: {', '.join(sorted(ctx.done)) or '(nothing)'}")
    else:
        warn_if_dirty(repo)
        ctx = make_context(repo, config, args.task, args.base_ref)
        prompts = snapshot(ctx, prompt_dirs(config, script_dir))

    print(f"\nRun ID:   {ctx.run_id}")
    print(f"Base:     {ctx.base_ref}")
    print(f"Branch:   {ctx.branch}")
    print(f"Worktree: {ctx.worktree}")
    print(f"Artifacts:{ctx.artifacts}")

    try:
        return run_stages(ctx, args, prompts)
    except (StargateError, KeyboardInterrupt) as exc:
        save_state(ctx, "failed", f"{type(exc).__name__}: {exc}")
        print(f"\nResume with: stargate resume {ctx.run_id}", file=sys.stderr)
        raise


def run_stages(ctx: RunContext, args: argparse.Namespace, prompts: list[Path]) -> int:
    config = ctx.config
    plan_path = ctx.artifacts / "plan.md"

    # 1. Architect reads the original repository and emits a plan.
    if "architect" in ctx.done:
        plan = plan_path.read_text().strip()
        print(f"\n=== ARCHITECT (skipped, reusing {plan_path}) ===")
    else:
        enter_stage(ctx, "architect")
        architect_prompt = render_prompt(
            prompts, "architect", task=ctx.task, base_ref=ctx.base_ref
        )
        print("\n=== ARCHITECT ===")
        plan = invoke_agent(ctx, "architect", architect_prompt, ctx.repo, plan_path).strip()
        if not plan:
            raise StargateError("Architect returned an empty plan.")
        complete_stage(ctx, "architect")

    # 2. Create isolated implementation branch/worktree.
    enter_stage(ctx, "worktree")
    print("\n=== WORKTREE ===")
    create_worktree(ctx)
    complete_stage(ctx, "worktree")

    if budget_spent(ctx, "the developer"):
        return finish(ctx, ctx.task, "BUDGET_EXCEEDED", None)

    # 3. Developer implements.
    if "developer" in ctx.done:
        print("\n=== DEVELOPER (skipped, already ran in this run) ===")
    else:
        enter_stage(ctx, "developer")
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
            return finish(ctx, ctx.task, "BUDGET_EXCEEDED", test_exit)
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
            return finish(ctx, ctx.task, "BUDGET_EXCEEDED", test_exit)

        print(f"\n=== FIXER {attempt + 1} ===")
        invoke_agent(
            ctx,
            "fixer",
            fixer_prompt,
            ctx.worktree,
            ctx.artifacts / f"fix-{attempt + 1}.txt",
        )
        test_exit, test_report = run_tests(ctx, f"fix-{attempt + 1}")

    complete_stage(ctx, "review")
    return finish(ctx, ctx.task, verdict, test_exit)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stargate",
        description="Tiny Claude Code + Codex CLI multi-agent orchestrator.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help=f"Config file. Default: ./{PROJECT_CONFIG}, then "
        "~/.config/stargate/agents.yaml, then the packaged defaults.",
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

    run = sub.add_parser("run", help="Plan, implement, review and fix a task.")
    run.add_argument("task", help="Feature/bug/task description.")
    run.add_argument(
        "--base-ref",
        default=None,
        help="Git ref to branch from. Defaults to the current branch/ref.",
    )

    resume = sub.add_parser(
        "resume",
        help="Continue a run that failed partway, reusing its plan, worktree, "
        "config and prompts.",
    )
    resume.add_argument("run_id", help="Run ID, as printed by the original run.")

    for parser_ in (run, resume):
        parser_.add_argument(
            "--max-review-loops",
            type=int,
            default=None,
            help="Override settings.max_review_loops.",
        )
    return parser


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "init-config":
        return init_config(script_dir)
    if args.command == "init-prompts":
        return init_prompts(script_dir)

    config_path = resolve_config(args.config, script_dir)

    try:
        config = load_config(config_path)
        if args.command == "doctor":
            return doctor(config, config_path, script_dir, probe=args.probe)
        if args.command in ("run", "resume"):
            return orchestrate(args, script_dir, config)
        parser.error("Unknown command")
        return 2
    except StargateError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
