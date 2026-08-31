#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
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
    tokens_used: int = 0


def run_process(
    args: list[str],
    cwd: Path,
    *,
    capture: bool = True,
    check: bool = True,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    print(f"\n$ {shlex.join(args)}", flush=True)
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.STDOUT if capture else None,
            timeout=timeout,
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


def doctor(config: dict[str, Any], config_path: Path, script_dir: Path) -> int:
    print("stargate doctor\n")
    print(f"Config:  {config_path}\n")
    ok = True
    binaries = {"git"}
    for role in ROLES:
        cmd = agent_command(config, role)
        binaries.add(cmd[0])

    for binary in sorted(binaries):
        path = shutil.which(binary)
        state = "OK" if path else "MISSING"
        print(f"{state:8} {binary:12} {path or ''}")
        ok = ok and bool(path)

    cap = token_cap(config)
    print(f"\nToken cap: {cap:,}" if cap else "\nToken cap: none")

    print("\nAgents:")
    for role in ROLES:
        meters = "reports usage" if agent_entry(config, role).get("usage_pattern") else "no usage_pattern"
        print(f"  {role:10} {shlex.join(agent_command(config, role))}")
        if cap:
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
    return find_prompt(dirs, name).read_text().format(**values)


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

    timeout = float(ctx.config.get("settings", {}).get("agent_timeout_seconds", 1800))
    proc = run_process([*cmd, prompt], cwd, capture=True, timeout=timeout or None)
    transcript = proc.stdout or ""

    used = parse_usage(transcript, agent_entry(ctx.config, role).get("usage_pattern"))
    ctx.tokens_used += used
    if used:
        cap = token_cap(ctx.config)
        budget = f" of {cap:,}" if cap else ""
        print(f"\n[{role}] reported {used:,} tokens; {ctx.tokens_used:,}{budget} used so far.")

    if not writes_final:
        output_path.write_text(transcript)
        return transcript

    output_path.with_name(output_path.name + ".log").write_text(transcript)
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


def create_worktree(ctx: RunContext) -> None:
    git(
        ctx.repo,
        "worktree",
        "add",
        "-b",
        ctx.branch,
        str(ctx.worktree),
        ctx.base_ref,
        capture=True,
    )


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
    warn_if_dirty(repo)
    ctx = make_context(repo, config, args.task, args.base_ref)

    prompts = prompt_dirs(config, script_dir)

    print(f"\nRun ID:   {ctx.run_id}")
    print(f"Base:     {ctx.base_ref}")
    print(f"Branch:   {ctx.branch}")
    print(f"Worktree: {ctx.worktree}")
    print(f"Artifacts:{ctx.artifacts}")

    # 1. Architect reads the original repository and emits a plan.
    architect_prompt = render_prompt(
        prompts, "architect", task=args.task, base_ref=ctx.base_ref
    )
    print("\n=== ARCHITECT ===")
    plan = invoke_agent(
        ctx, "architect", architect_prompt, repo, ctx.artifacts / "plan.md"
    ).strip()
    if not plan:
        raise StargateError("Architect returned an empty plan.")

    # 2. Create isolated implementation branch/worktree.
    print("\n=== WORKTREE ===")
    create_worktree(ctx)

    if budget_spent(ctx, "the developer"):
        return finish(ctx, args.task, "BUDGET_EXCEEDED", None)

    # 3. Developer implements.
    developer_prompt = render_prompt(
        prompts,
        "developer",
        task=args.task,
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

    test_exit, test_report = run_tests(ctx, "developer")

    # 4. Review/fix loop.
    configured_loops = int(config.get("settings", {}).get("max_review_loops", 2))
    max_loops = args.max_review_loops if args.max_review_loops is not None else configured_loops
    verdict = "CHANGES_REQUESTED"

    for attempt in range(max_loops + 1):
        if budget_spent(ctx, f"review {attempt + 1}"):
            return finish(ctx, args.task, "BUDGET_EXCEEDED", test_exit)
        review_prompt = render_prompt(
            prompts,
            "reviewer",
            task=args.task,
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
            task=args.task,
            base_ref=ctx.base_ref,
            plan=plan,
            review=review,
            tests=test_report,
        )
        if budget_spent(ctx, f"fixer {attempt + 1}"):
            return finish(ctx, args.task, "BUDGET_EXCEEDED", test_exit)

        print(f"\n=== FIXER {attempt + 1} ===")
        invoke_agent(
            ctx,
            "fixer",
            fixer_prompt,
            ctx.worktree,
            ctx.artifacts / f"fix-{attempt + 1}.txt",
        )
        test_exit, test_report = run_tests(ctx, f"fix-{attempt + 1}")

    return finish(ctx, args.task, verdict, test_exit)


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

    sub.add_parser("doctor", help="Check local CLI dependencies and configuration.")
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
    run.add_argument(
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
            return doctor(config, config_path, script_dir)
        if args.command == "run":
            return orchestrate(args, script_dir, config)
        parser.error("Unknown command")
        return 2
    except StargateError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
