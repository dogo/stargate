"""Reporting which coding-agent CLIs are installed but unused by this config."""
from __future__ import annotations

import os
import stat
from pathlib import Path
from unittest.mock import patch

from stargate.doctor import available_agent_clis


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
