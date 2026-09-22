"""Role selection must preserve verified capabilities without invoking agents."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import yaml

from stargate.detect import detection_mode
from stargate.doctor import AGENT_CLI_WRAPPERS, WRAPPER_EXTRA_BINARIES
from stargate.wizard import init_config
from tests.harness import ROOT, fake_bin, make_repo, stargate, stargate_tty

PACKAGE = ROOT / "stargate"


def test_init_config_import_order_cannot_break_the_lint_gate(root: Path) -> None:
    # A make-test-only run must also catch the import error missed by the prior run.
    proc = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--no-cache", "--select", "I",
         "tests/test_init_config.py"],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_generated_config_documents_packaged_settings_without_overriding_them(root: Path) -> None:
    repo = make_repo(root)
    bindir = Path(fake_bin(root, "codex"))
    (bindir / "git").symlink_to(shutil.which("git"))
    env = {"PATH": str(bindir)}
    proc = stargate_tty(repo, "init-config", config_home=root, answers="\n" * 4, env=env)
    assert proc.returncode == 0, proc.stdout
    text = (root / "stargate/agents.yaml").read_text()
    packaged = (PACKAGE / "agents.yaml").read_text()
    settings = packaged[packaged.index("\nsettings:\n") + 1:]
    documented = "\n".join(f"# {line}".rstrip() for line in settings.splitlines())
    assert documented in text, proc.stdout + text
    for setting in ('test_command: ""', "test_command_detection: report",
                    "agent_timeout_seconds: 1800", "max_task_tokens: 0"):
        assert f"#   {setting}" in text, proc.stdout + text
    assert "settings" not in yaml.safe_load(text), proc.stdout + text
    proc = stargate(repo, "doctor", config_home=root, env=env)
    assert proc.returncode == 0, proc.stdout
    # [2] proves the default still comes from the packaged layer, not the new file.
    assert any(line.split() == ["max_review_loops", "2", "[2]"]
               for line in proc.stdout.splitlines()), proc.stdout


def test_corrupt_packaged_vendors_reports_a_clean_error_instead_of_a_traceback(root: Path) -> None:
    package = root / "pkg" / "stargate"
    shutil.copytree(PACKAGE, package, ignore=shutil.ignore_patterns("__pycache__"))
    (package / "vendors.yaml").write_text(
        "version: 1\nvendors:\n  broken:\n    description: two writers, no reader\n"
        "    agents:\n"
        "      broken_one: {command: [claude], probe_expect: write}\n"
        "      broken_two: {command: [claude], probe_expect: write}\n"
    )
    # Setup must reach its own error handler even outside a Git repository.
    proc = stargate(root, "init-config", config_home=root,
                    env={"PYTHONPATH": str(root / "pkg"), "PATH": fake_bin(root)})
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "ERROR: Packaged vendor broken needs one reader and one writer" in proc.stderr, (
        proc.stdout + proc.stderr
    )
    assert "Traceback" not in proc.stdout + proc.stderr, proc.stdout + proc.stderr
    assert not (root / "stargate").exists(), proc.stdout + proc.stderr


def test_packaged_vendor_blocks_stay_identical_to_the_verified_examples(root: Path) -> None:
    vendors = yaml.safe_load((PACKAGE / "vendors.yaml").read_text())["vendors"]
    examples = {path.parent.name: path for path in (ROOT / "examples").glob("*/agents.yaml")}
    assert vendors.keys() == examples.keys()
    for name, path in examples.items():
        assert vendors[name]["agents"] == yaml.safe_load(path.read_text())["agents"], name
    assert '"vendors.yaml"' in (ROOT / "pyproject.toml").read_text()


def test_an_example_disabling_detection_is_not_read_back_as_the_report_default(
    root: Path,
) -> None:
    # Bare off is a YAML boolean: detection_mode silently turns False into
    # report, so the example would still report a guessed test command.
    kiro = yaml.safe_load((ROOT / "examples/kiro/agents.yaml").read_text())
    assert detection_mode(kiro) == "off", kiro["settings"]
    for path in sorted((ROOT / "examples").glob("*/agents.yaml")):
        configured = yaml.safe_load(path.read_text()).get("settings", {}).get(
            "test_command_detection"
        )
        if configured is None:
            continue
        assert isinstance(configured, str), f"{path}: {configured!r}"


def test_opencode_without_its_wrapper_cannot_produce_an_unrunnable_opencode_config(
    root: Path,
) -> None:
    # Bare opencode is silent into stargate's regular trace file. Offering it
    # without the wrapper would produce a config that hangs instead of answering.
    proc = stargate_tty(root, "init-config", config_home=root, answers="",
                        env={"PATH": fake_bin(root, "opencode")})
    assert proc.returncode == 0, proc.stdout
    assert "opencode found" in proc.stdout, proc.stdout
    assert "MISSING opencode-stargate" in proc.stdout, proc.stdout
    assert "architect [" not in proc.stdout, proc.stdout
    assert (root / "stargate/agents.yaml").read_bytes() == (
        PACKAGE / "agents.yaml"
    ).read_bytes(), proc.stdout


def test_opencode_is_not_offered_when_the_python3_its_wrapper_runs_is_absent(root: Path) -> None:
    # The wrapper and CLI resolve, but every invocation would fail inside the
    # wrapper. Stargate itself runs through sys.executable, independently of PATH.
    proc = stargate_tty(root, "init-config", config_home=root, answers="",
                        env={"PATH": fake_bin(root, "opencode-stargate", "opencode")})
    assert proc.returncode == 0, proc.stdout
    assert "architect [" not in proc.stdout, proc.stdout
    assert "MISSING python3 (required by opencode-stargate)" in proc.stdout, proc.stdout
    assert "but the CLI it runs is not" not in proc.stdout, proc.stdout
    assert (root / "stargate/agents.yaml").read_bytes() == (
        PACKAGE / "agents.yaml"
    ).read_bytes(), proc.stdout


def test_a_missing_wrapper_dependency_is_named_with_the_wrapper_that_needs_it(root: Path) -> None:
    # The diagnostic must also appear when another vendor keeps the menu usable.
    proc = stargate_tty(root, "init-config", config_home=root, answers="\n" * 4,
                        env={"PATH": fake_bin(root, "codex", "opencode-stargate", "opencode")})
    assert proc.returncode == 0, proc.stdout
    assert "MISSING python3 (required by opencode-stargate)" in proc.stdout, proc.stdout
    assert "but the CLI it runs is not" not in proc.stdout, proc.stdout
    assert "opencode: opencode through opencode-stargate" not in proc.stdout, proc.stdout
    config = yaml.safe_load((root / "stargate/agents.yaml").read_text())
    assert set(config["workflow"].values()) == {"codex_reader", "codex_writer"}, proc.stdout


def test_opencode_is_offered_when_its_wrapper_cli_and_python3_all_resolve(root: Path) -> None:
    proc = stargate_tty(root, "init-config", config_home=root, answers="\n" * 4,
                        env={"PATH": fake_bin(root, "opencode-stargate", "opencode", "python3")})
    assert proc.returncode == 0, proc.stdout
    assert "opencode: opencode through opencode-stargate" in proc.stdout, proc.stdout
    assert "MISSING python3" not in proc.stdout, proc.stdout
    config = yaml.safe_load((root / "stargate/agents.yaml").read_text())
    assert config["workflow"] == dict(
        architect="opencode_reader", developer="opencode_writer",
        reviewer="opencode_reader", fixer="opencode_writer",
    ), proc.stdout
    assert set(config["agents"]) == {"opencode_reader", "opencode_writer"}, proc.stdout


def test_a_declared_wrapper_dependency_belongs_to_a_shipped_wrapper(root: Path) -> None:
    assert set(WRAPPER_EXTRA_BINARIES) <= set(AGENT_CLI_WRAPPERS)


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
    # Gemini is now verified; Cursor remains detection-only.
    proc = stargate_tty(root, "init-config", config_home=root,
                        answers="cursor-agent\n0\n99\n\n\n\n\n",
                        env={"PATH": fake_bin(root, "codex", "cursor-agent")})
    assert proc.returncode == 0, proc.stdout
    assert "not configurable by init-config: cursor-agent" in proc.stdout, proc.stdout
    assert proc.stdout.count("Choose a listed number") == 3, proc.stdout
    assert "1. cursor-agent" not in proc.stdout, proc.stdout
    config = yaml.safe_load((root / "stargate/agents.yaml").read_text())
    assert set(config["workflow"].values()) == {"codex_reader", "codex_writer"}, proc.stdout


def test_a_wrapper_and_the_cli_it_runs_together_offer_the_verified_choices(root: Path) -> None:
    proc = stargate_tty(root, "init-config", config_home=root, answers="\n" * 4,
                        env={"PATH": fake_bin(root, "kiro-stargate", "kiro-cli")})
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


def test_gemini_is_offered_and_fills_every_role_when_only_its_cli_is_installed(root: Path) -> None:
    # Installing the verified executable must make both capabilities reachable.
    proc = stargate_tty(root, "init-config", config_home=root, answers="\n" * 4,
                        env={"PATH": fake_bin(root, "gemini")})
    assert proc.returncode == 0, proc.stdout
    assert "gemini: Gemini CLI (examples/gemini)" in proc.stdout, proc.stdout
    config = yaml.safe_load((root / "stargate/agents.yaml").read_text())
    assert config["workflow"] == dict(architect="gemini_reader", developer="gemini_writer",
                                      reviewer="gemini_reader", fixer="gemini_writer"), proc.stdout
    # Plan mode cannot run tests, so it must not get a separate reviewer grant.
    assert set(config["agents"]) == {"gemini_reader", "gemini_writer"}, proc.stdout
    for name, mode in (("gemini_reader", "plan"), ("gemini_writer", "auto_edit")):
        assert config["agents"][name]["command"] == [
            "gemini", "--output-format", "text", "--approval-mode", mode,
            "--skip-trust", "--prompt",
        ], proc.stdout


def test_gemini_is_not_offered_when_its_executable_is_absent(root: Path) -> None:
    # A catalog entry alone must not offer a config whose first run would fail.
    proc = stargate_tty(root, "init-config", config_home=root, answers="gemini\n\n\n\n\n",
                        env={"PATH": fake_bin(root, "codex")})
    assert proc.returncode == 0, proc.stdout
    assert "gemini" not in proc.stdout, proc.stdout
    assert "Choose a listed number" in proc.stdout, proc.stdout
    config = yaml.safe_load((root / "stargate/agents.yaml").read_text())
    assert set(config["workflow"].values()) == {"codex_reader", "codex_writer"}, proc.stdout


def test_a_path_resolving_wrapper_without_its_cli_does_not_make_the_vendor_selectable(
    root: Path,
) -> None:
    # The wrapper invokes opencode through PATH; without it the config cannot run.
    proc = stargate_tty(root, "init-config", config_home=root, answers="",
                        env={"PATH": fake_bin(root, "opencode-stargate")})
    assert proc.returncode == 0, proc.stdout
    assert ("MISSING opencode: the examples/opencode wrapper is on PATH, "
            "but the CLI it runs is not.") in proc.stdout, proc.stdout
    assert "MISSING opencode-stargate" not in proc.stdout, proc.stdout
    assert "architect [" not in proc.stdout, proc.stdout
    assert (root / "stargate/agents.yaml").read_bytes() == (
        PACKAGE / "agents.yaml"
    ).read_bytes(), proc.stdout


def test_a_wrapper_with_its_own_cli_lookup_stays_selectable_without_the_cli_on_path(
    root: Path,
) -> None:
    # Kiro uses KIRO_BIN or its app-bundle path. Requiring kiro-cli on PATH
    # rejects the macOS installs the wrapper was written for.
    proc = stargate_tty(root, "init-config", config_home=root, answers="\n" * 4,
                        env={"PATH": fake_bin(root, "kiro-stargate")})
    assert proc.returncode == 0, proc.stdout
    assert "kiro: Kiro CLI through kiro-stargate (examples/kiro)" in proc.stdout, proc.stdout
    assert "MISSING kiro-cli" not in proc.stdout, proc.stdout
    config = yaml.safe_load((root / "stargate/agents.yaml").read_text())
    assert config["workflow"] == dict(architect="kiro_reader", developer="kiro_writer",
                                      reviewer="kiro_reader", fixer="kiro_writer"), proc.stdout
