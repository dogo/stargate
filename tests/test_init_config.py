"""Role selection must preserve verified capabilities without invoking agents."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from unittest.mock import patch

import yaml

from stargate.wizard import init_config
from tests.harness import ROOT, fake_bin, make_repo, stargate, stargate_tty

PACKAGE = ROOT / "stargate"


def test_packaged_vendor_blocks_stay_identical_to_the_verified_examples(root: Path) -> None:
    vendors = yaml.safe_load((PACKAGE / "vendors.yaml").read_text())["vendors"]
    examples = {path.parent.name: path for path in (ROOT / "examples").glob("*/agents.yaml")}
    assert vendors.keys() == examples.keys()
    for name, path in examples.items():
        assert vendors[name]["agents"] == yaml.safe_load(path.read_text())["agents"], name
    assert '"vendors.yaml"' in (ROOT / "pyproject.toml").read_text()


def test_the_claude_reviewer_preserves_the_packaged_defaults_grant_verbatim(root: Path) -> None:
    vendors = yaml.safe_load((PACKAGE / "vendors.yaml").read_text())["vendors"]
    packaged = yaml.safe_load((PACKAGE / "agents.yaml").read_text())
    assert vendors["claude"]["reviewer_command"] == packaged["agents"]["reviewer"]["command"]


def test_nonterminal_input_copies_the_packaged_default_without_asking_roles(root: Path) -> None:
    proc = stargate(root, "init-config", config_home=root, stdin="codex\n" * 4,
                    env={"PATH": fake_bin(root, "claude", "codex")})
    assert proc.returncode == 0, proc.stdout
    assert "stdin is not a terminal" in proc.stdout, proc.stdout
    assert (root / "stargate/agents.yaml").read_bytes() == (
        PACKAGE / "agents.yaml"
    ).read_bytes(), proc.stdout
    assert "architect [" not in proc.stdout, proc.stdout


def test_kiro_cli_without_its_wrapper_cannot_produce_an_unrunnable_kiro_config(root: Path) -> None:
    proc = stargate_tty(root, "init-config", config_home=root, answers="",
                        env={"PATH": fake_bin(root, "kiro-cli")})
    assert proc.returncode == 0, proc.stdout
    assert "kiro-cli found" in proc.stdout, proc.stdout
    for binary in ("claude", "codex", "kiro-stargate"):
        assert f"MISSING {binary}" in proc.stdout, proc.stdout
    assert (root / "stargate/agents.yaml").read_bytes() == (
        PACKAGE / "agents.yaml"
    ).read_bytes(), proc.stdout


def test_each_role_gets_the_verified_block_for_the_capability_it_needs(root: Path) -> None:
    proc = stargate_tty(root, "init-config", config_home=root,
                        answers="2\nCLAUDE\ncodex\n1\n",
                        env={"PATH": fake_bin(root, "claude", "codex")})
    assert proc.returncode == 0, proc.stdout
    config = yaml.safe_load((root / "stargate/agents.yaml").read_text())
    assert config["workflow"] == dict(architect="codex_reader", developer="claude_writer",
                                      reviewer="codex_reader", fixer="claude_writer"), proc.stdout
    vendors = yaml.safe_load((PACKAGE / "vendors.yaml").read_text())["vendors"]
    for role, name in config["workflow"].items():
        block = config["agents"][name]
        assert block == vendors[name.split("_")[0]]["agents"][name], proc.stdout
        assert block["probe_expect"] == (
            "read" if role in ("architect", "reviewer") else "write"
        ), proc.stdout
    assert set(config["agents"]) == set(config["workflow"].values()), proc.stdout


def test_enter_preserves_default_vendors_and_only_the_claude_reviewer_gets_test_execution(
    root: Path,
) -> None:
    proc = stargate_tty(root, "init-config", config_home=root, answers="\n" * 4,
                        env={"PATH": fake_bin(root, "claude", "codex")})
    assert proc.returncode == 0, proc.stdout
    config = yaml.safe_load((root / "stargate/agents.yaml").read_text())
    assert config["workflow"] == dict(architect="claude_reader", developer="codex_writer",
                                      reviewer="claude_reviewer", fixer="codex_writer"), proc.stdout
    reviewer = config["agents"]["claude_reviewer"]["command"]
    assert "Bash({test_command})" in reviewer, proc.stdout
    assert reviewer[-2:] == ["--model", "opus"], proc.stdout
    assert "Bash({test_command})" not in config["agents"]["claude_reader"]["command"], proc.stdout


def test_existing_hand_edited_config_requires_force_and_keeps_each_backup(root: Path) -> None:
    target = root / "stargate/agents.yaml"
    target.parent.mkdir()
    target.write_text("hand edited\n")
    env = {"PATH": fake_bin(root)}
    proc = stargate(root, "init-config", config_home=root, env=env)
    assert proc.returncode == 1, proc.stdout
    assert "--force" in proc.stdout, proc.stdout
    assert target.read_text() == "hand edited\n", proc.stdout
    for text in ("hand edited\n", "edited again\n"):
        target.write_text(text)
        proc = stargate(root, "init-config", "--force", config_home=root, env=env)
        assert proc.returncode == 0, proc.stdout
        backups = list(target.parent.glob("*.bak"))
        matching = [path for path in backups if path.read_text() == text]
        assert len(matching) == 1, proc.stdout
        assert str(matching[0]) in proc.stdout, proc.stdout
        assert target.read_bytes() == (PACKAGE / "agents.yaml").read_bytes(), proc.stdout
    assert len(backups) == 2, proc.stdout


def test_generated_config_loads_through_normal_layering_without_running_agents(root: Path) -> None:
    repo = make_repo(root)
    bindir = Path(fake_bin(root, "claude", "codex"))
    # Doctor needs Git. Vendor stubs leave evidence if setup accidentally probes.
    (bindir / "git").symlink_to(shutil.which("git"))
    marker = root / "agent-was-run"
    for name in ("claude", "codex"):
        (bindir / name).write_text(f"#!/bin/sh\necho called > '{marker}'\nexit 1\n")
    env = {"PATH": str(bindir)}
    proc = stargate_tty(repo, "init-config", config_home=root, answers="\n" * 4, env=env)
    assert proc.returncode == 0, proc.stdout
    proc = stargate(repo, "doctor", config_home=root, env=env)
    assert proc.returncode == 0, proc.stdout
    for role, binary in (("architect", "claude"), ("developer", "codex"),
                         ("reviewer", "claude"), ("fixer", "codex")):
        assert any(line.split()[:3] == [role, "[1]", binary]
                   for line in proc.stdout.splitlines()), proc.stdout
    assert not marker.exists(), proc.stdout


def test_unverified_clis_are_reported_but_cannot_be_selected(root: Path) -> None:
    proc = stargate_tty(root, "init-config", config_home=root,
                        answers="gemini\n0\n99\n\n\n\n\n",
                        env={"PATH": fake_bin(root, "codex", "gemini")})
    assert proc.returncode == 0, proc.stdout
    assert "not configurable by init-config: gemini" in proc.stdout, proc.stdout
    assert proc.stdout.count("Choose a listed number") == 3, proc.stdout
    assert "1. gemini" not in proc.stdout, proc.stdout
    config = yaml.safe_load((root / "stargate/agents.yaml").read_text())
    assert set(config["workflow"].values()) == {"codex_reader", "codex_writer"}, proc.stdout


def test_kiro_wrapper_is_sufficient_for_verified_reader_and_writer_choices(root: Path) -> None:
    proc = stargate_tty(root, "init-config", config_home=root, answers="\n" * 4,
                        env={"PATH": fake_bin(root, "kiro-stargate")})
    assert proc.returncode == 0, proc.stdout
    config = yaml.safe_load((root / "stargate/agents.yaml").read_text())
    assert config["workflow"] == dict(architect="kiro_reader", developer="kiro_writer",
                                      reviewer="kiro_reader", fixer="kiro_writer"), proc.stdout
    assert "settings" not in config, proc.stdout


def test_terminal_eof_defaults_remaining_roles_without_repeating_questions(root: Path) -> None:
    proc = stargate_tty(root, "init-config", config_home=root, answers="\x04",
                        env={"PATH": fake_bin(root, "codex")})
    assert proc.returncode == 0, proc.stdout
    assert "Input ended; remaining roles take their defaults" in proc.stdout, proc.stdout
    assert "developer [" not in proc.stdout, proc.stdout
    config = yaml.safe_load((root / "stargate/agents.yaml").read_text())
    assert len(config["workflow"]) == 4, proc.stdout


def test_interrupting_role_selection_preserves_existing_config_without_a_backup(root: Path) -> None:
    target = root / "stargate/agents.yaml"
    target.parent.mkdir()
    target.write_text("hand edited\n")
    with (
        patch.dict(os.environ, {"XDG_CONFIG_HOME": str(root), "PATH": fake_bin(root, "codex")}),
        patch("sys.stdin.isatty", return_value=True),
        patch("builtins.input", side_effect=KeyboardInterrupt),
    ):
        assert init_config(PACKAGE, force=True) == 130
    assert target.read_text() == "hand edited\n"
    assert not list(target.parent.glob("*.bak"))
