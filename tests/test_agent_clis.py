"""Reporting which coding-agent CLIs are installed but unused by this config."""
from __future__ import annotations

import os
import stat
from pathlib import Path
from unittest.mock import patch

from stargate.doctor import available_agent_clis
from tests.harness import doctor, make_repo, write_config


def fake_bin(root: Path, *names: str) -> str:
    """A PATH containing only executables with these names."""
    bindir = root / "bin"
    bindir.mkdir(exist_ok=True)
    for name in names:
        exe = bindir / name
        exe.write_text("#!/bin/sh\nexit 0\n")
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return str(bindir)


def test_installed_agent_cli_is_reported_when_the_config_ignores_it(root: Path) -> None:
    # The machine running stargate need not have the vendors the packaged
    # config names. Naming what IS there is what turns "MISSING claude" from a
    # dead end into a config the user can write.
    with patch.dict(os.environ, {"PATH": fake_bin(root, "gemini", "cursor-agent")}):
        found = available_agent_clis({"git", "claude", "codex"})
    assert [name for name, _, _ in found] == ["cursor-agent", "gemini"], found


def test_configured_cli_is_not_offered_as_an_alternative(root: Path) -> None:
    # Listing an agent the config already drives would read as a suggestion to
    # change something that is already working.
    with patch.dict(os.environ, {"PATH": fake_bin(root, "codex", "gemini")}):
        found = available_agent_clis({"git", "codex"})
    assert [name for name, _, _ in found] == ["gemini"], found


def test_a_cli_configured_by_absolute_path_is_not_offered_as_an_alternative(
    root: Path,
) -> None:
    # An agent command is an argument list, and its first element may be a
    # path. Comparing it whole against the executable name reported the very
    # CLI the config drives as something the user could switch to.
    bindir = fake_bin(root, "gemini")
    with patch.dict(os.environ, {"PATH": bindir}):
        found = available_agent_clis({"git", f"{bindir}/gemini"})
    assert found == [], found


def test_an_installed_kiro_cli_is_named_by_the_executable_vendors_ship(root: Path) -> None:
    # examples/kiro is verified against the `kiro-cli` executable; keying the
    # table on `kiro` meant the one vendor the repository ships a wrapper for
    # was the one discovery never found.
    with patch.dict(os.environ, {"PATH": fake_bin(root, "kiro-cli")}):
        found = available_agent_clis({"git"})
    assert [name for name, _, _ in found] == ["kiro-cli"], found


def test_a_shipped_wrapper_counts_as_the_vendor_it_calls(root: Path) -> None:
    # examples/kiro drives Kiro through `kiro-stargate`, which execs kiro-cli.
    # Naming kiro-cli as an unused alternative there advises switching away
    # from the one configuration this repository verified against that vendor.
    with patch.dict(os.environ, {"PATH": fake_bin(root, "kiro-cli", "kiro-stargate")}):
        found = available_agent_clis({"git", "kiro-stargate"})
    assert found == [], found


def test_a_configured_path_that_does_not_exist_still_points_at_the_cli_on_path(
    root: Path,
) -> None:
    # `MISSING /opt/gemini/bin/gemini` with a working `gemini` one directory
    # away is exactly the dead end this report exists to end; the basename
    # match must not hide an alternative for a binary that is not there.
    bindir = fake_bin(root, "gemini")
    with patch.dict(os.environ, {"PATH": bindir}):
        found = available_agent_clis({"git", "/opt/gemini/bin/gemini"})
    assert [(name, path) for name, path, _ in found] == [
        ("gemini", f"{bindir}/gemini")
    ], found


def test_doctor_reports_the_unused_clis_below_the_binary_report(root: Path) -> None:
    # The helper being right is not the feature: doctor has to call it, and
    # print it where a `MISSING` line sends the reader looking -- after the
    # FOUND/MISSING block, not among it.
    repo = make_repo(root)
    config = root / "agents.yaml"
    write_config(config, 'echo "VERDICT: APPROVED"', test_command="true")
    bindir = fake_bin(root, "gemini")
    proc = doctor(repo, config, env={"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"})

    assert proc.returncode == 0, proc.stdout + proc.stderr
    header = "Other agent CLIs on PATH, not used by this config:"
    assert header in proc.stdout, proc.stdout
    assert f"gemini       {bindir}/gemini  -- Gemini CLI" in proc.stdout, proc.stdout
    assert proc.stdout.index(header) > proc.stdout.rindex("FOUND    "), proc.stdout
