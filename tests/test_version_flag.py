"""Version reporting must work before config loading or repository discovery."""

from __future__ import annotations

from importlib import metadata
from pathlib import Path
from unittest.mock import patch

from stargate.cli import build_parser
from tests.harness import stargate


def test_version_prints_installed_metadata_outside_a_git_repo(root: Path) -> None:
    try:
        expected = metadata.version("stargate-cli")
    except metadata.PackageNotFoundError:
        expected = "unknown (source checkout)"

    for args in ((), ("--config", str(root / "missing.yaml"))):
        proc = stargate(root, *args, "--version", config_home=root / "config-home")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert proc.stderr == "", proc.stderr
        assert proc.stdout == f"stargate {expected}\n", proc.stdout
        assert not (root / ".stargate").exists()
        assert not (root / "config-home").exists()


def test_top_level_help_lists_the_version_option(root: Path) -> None:
    proc = stargate(root, "--help", config_home=root / "config-home")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stderr == "", proc.stderr
    assert "--version" in proc.stdout, proc.stdout


def test_missing_distribution_metadata_leaves_the_cli_usable(root: Path) -> None:
    del root
    with patch("stargate.cli.metadata.version", side_effect=metadata.PackageNotFoundError):
        assert build_parser().parse_args(["list"]).command == "list"
