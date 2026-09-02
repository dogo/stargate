"""Everything read off disk to configure a run: the layered YAML, the agent
entry each role resolves to, and the prompt files."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from .core import StargateError


# Per-project override. Deliberately NOT "agents.yaml": that name is common
# enough that a global install would silently pick up an unrelated repo's file.
PROJECT_CONFIG = ".stargate.yaml"


ROLES = ("architect", "developer", "reviewer", "fixer")


def user_config() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(os.path.expanduser(base)) / "stargate" / "agents.yaml"


def resolve_config(arg: str | None, script_dir: Path) -> list[Path]:
    """Config sources, most specific first."""
    if arg:
        # Explicit config is also the escape hatch for resuming past a broken
        # definition, so layering anything under it would make it non-explicit.
        return [Path(arg).expanduser().resolve()]

    project = (Path.cwd() / PROJECT_CONFIG).resolve()
    user = user_config().resolve()
    packaged = (script_dir / "agents.yaml").resolve()
    candidates = [path for path in (project, user) if path.exists()]
    # Lookup never walks to a parent or follows a path from config, so a project
    # cannot accidentally pull configuration from an unrelated repository.
    return list(dict.fromkeys([*candidates, packaged]))


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


def layer_config(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge sections by key while replacing each structured entry whole."""
    merged = dict(base)
    for section, value in override.items():
        inherited = merged.get(section)
        # A bare mapping section such as `settings:` must not erase its base.
        # ponytail: add a delete sentinel only if an overlay needs one.
        if isinstance(inherited, dict) and value is None:
            continue
        if isinstance(inherited, dict) and isinstance(value, dict):
            merged[section] = {**inherited, **value}
        else:
            merged[section] = value
    return merged


def load_config(
    paths: list[Path],
) -> tuple[dict[str, Any], list[tuple[Path, dict[str, Any]]]]:
    """Load the effective config and retain the layers that supplied it."""
    layers: list[tuple[Path, dict[str, Any]]] = []
    for path in paths:
        if not path.exists():
            raise StargateError(f"Config not found: {path}")
        data = yaml.safe_load(path.read_text()) or {}
        if not isinstance(data, dict):
            raise StargateError(f"Config must be a YAML mapping: {path}")
        layers.append((path, data))

    config: dict[str, Any] = {}
    for _, data in reversed(layers):
        config = layer_config(config, data)

    agents = config.get("agents")
    workflow = config.get("workflow")
    if (
        not isinstance(agents, dict) or not agents
        or not isinstance(workflow, dict) or not workflow
    ):
        raise StargateError("Config must contain 'agents' and 'workflow'.")
    if "settings" in config and not isinstance(config["settings"], dict):
        raise StargateError("Config 'settings' must be a mapping.")
    return config, layers


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


TEST_COMMAND_PLACEHOLDER = "{test_command}"


# The packaged allowlist syntax uses parentheses and commas as structure and
# `*` as a wildcard. Interpolating any of them would grant a PATTERN broader
# than the one project command stargate runs, even though config is trusted.
TEST_COMMAND_PATTERN_UNSAFE = re.compile(r"[(),*\x00-\x1f\x7f]")


def test_command_grant(test_command: str) -> str | None:
    """The exact command safe to place in a permission pattern, if any."""
    command = (test_command or "").strip()
    if not command or TEST_COMMAND_PATTERN_UNSAFE.search(command):
        return None
    return command


def expand_test_command(command: list[str], test_command: str) -> list[str]:
    """Expand {test_command}, dropping its whole option when no grant is safe.

    An empty value is not safe here: it can become Bash(), an empty argv item,
    or leave an option to consume the agent prompt as its value. The command
    placeholder therefore belongs in an option value, never in argv[0].
    """
    grant = test_command_grant(test_command)
    expanded: list[str] = []
    for index, part in enumerate(command):
        if TEST_COMMAND_PLACEHOLDER not in part:
            expanded.append(part)
            continue
        if index == 0:
            raise StargateError(
                "{test_command} cannot be used as an agent executable; put it "
                "in an option value."
            )
        if grant is not None:
            expanded.append(part.replace(TEST_COMMAND_PLACEHOLDER, grant))
            continue
        # An option containing the placeholder is self-contained, regardless
        # of its spelling. A separate value may itself contain `=`, so only
        # its position -- not that character -- identifies the option to drop.
        if not part.startswith("-") and expanded and expanded[-1].startswith("-"):
            expanded.pop()
    if not expanded:
        raise StargateError("Expanding {test_command} left an empty agent command.")
    return expanded


AGENT_RETRIES_DEFAULT = 0


AGENT_RETRY_BACKOFF_DEFAULT = 10.0


def retry_settings(config: dict[str, Any]) -> tuple[int, float]:
    settings = config.get("settings", {})
    retries = max(0, int(settings.get("agent_retries", AGENT_RETRIES_DEFAULT) or 0))
    backoff = max(0.0, float(
        settings.get(
            "agent_retry_backoff_seconds", AGENT_RETRY_BACKOFF_DEFAULT
        ) or 0
    ))
    return retries, backoff


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


def value_source(
    layers: list[tuple[Path, dict[str, Any]]], section: str, key: str
) -> str:
    """The numbered layer that supplied one effective value."""
    for index, (_, data) in enumerate(layers, 1):
        block = data.get(section)
        if isinstance(block, dict) and key in block:
            return f"[{index}]"
    return "(default)"


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
