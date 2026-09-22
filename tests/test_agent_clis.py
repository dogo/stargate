"""Required executables in agent environments and installed but unused CLIs."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from unittest.mock import patch

import yaml

from stargate.doctor import available_agent_clis
from tests.harness import doctor, fake_bin, make_repo, write_config


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


def _wrapper_only_path(root: Path, wrapper: str) -> str:
    bindir = fake_bin(root, wrapper)
    git = shutil.which("git")
    assert git is not None, "the integration tests require git"
    (Path(bindir) / "git").symlink_to(git)
    return bindir


def test_a_wrapper_on_path_does_not_hide_the_missing_cli_it_runs(root: Path) -> None:
    repo = make_repo(root)
    config = root / "agents.yaml"
    write_config(config, 'echo "VERDICT: APPROVED"', test_command="true")
    cfg = yaml.safe_load(config.read_text())
    bindir = _wrapper_only_path(root, "claude-json-stargate")
    # Both a bare command and an absolute wrapper path imply the CLI dependency.
    for head in ("claude-json-stargate", str(Path(bindir) / "claude-json-stargate")):
        cfg["agents"]["noop"]["command"] = [head, "--print"]
        config.write_text(yaml.safe_dump(cfg))
        proc = doctor(repo, config, env={"PATH": bindir})

        assert proc.returncode == 1, proc.stdout
        assert any(
            line.split()[:2] == ["MISSING", "claude"]
            and "required by claude-json-stargate" in line
            for line in proc.stdout.splitlines()
        ), proc.stdout
        assert any(
            line.split()[:2] == ["FOUND", head] for line in proc.stdout.splitlines()
        ), proc.stdout


def test_a_direct_cli_requirement_is_not_attributed_only_to_a_wrapper(root: Path) -> None:
    repo = make_repo(root)
    config = root / "agents.yaml"
    write_config(config, 'echo "VERDICT: APPROVED"', test_command="true")
    cfg = yaml.safe_load(config.read_text())
    cfg["agents"]["noop"]["command"] = ["claude-json-stargate"]
    cfg["agents"]["direct"] = {"command": ["claude"]}
    cfg["workflow"]["developer"] = "direct"
    config.write_text(yaml.safe_dump(cfg))
    bindir = _wrapper_only_path(root, "claude-json-stargate")
    proc = doctor(repo, config, env={"PATH": bindir})

    assert proc.returncode == 1, proc.stdout
    assert any(
        line.split()[:2] == ["MISSING", "claude"]
        and "configured directly; also required by claude-json-stargate" in line
        for line in proc.stdout.splitlines()
    ), proc.stdout


def test_a_wrapper_that_resolves_its_own_cli_is_not_required_on_path(root: Path) -> None:
    repo = make_repo(root)
    config = root / "agents.yaml"
    write_config(config, 'echo "VERDICT: APPROVED"', test_command="true")
    cfg = yaml.safe_load(config.read_text())
    cfg["agents"]["noop"]["command"] = ["kiro-stargate"]
    config.write_text(yaml.safe_dump(cfg))
    bindir = _wrapper_only_path(root, "kiro-stargate")
    proc = doctor(repo, config, env={"PATH": bindir})

    assert proc.returncode == 0, proc.stdout
    assert "kiro-cli" not in proc.stdout, proc.stdout


def test_a_cli_only_on_the_agents_own_path_is_not_reported_missing(root: Path) -> None:
    # A private PATH can make an agent work even though doctor cannot see its
    # CLI. This applies to both the command head and a wrapper's dependency.
    repo = make_repo(root)
    config = root / "agents.yaml"
    write_config(config, 'echo "VERDICT: APPROVED"', test_command="true")
    cfg = yaml.safe_load(config.read_text())
    private = root / "private"
    private.mkdir()
    bindir = fake_bin(private, "vendor-cli", "claude-json-stargate", "claude")
    doctor_path = _wrapper_only_path(root, "unused-cli")
    cfg["agents"]["noop"]["env"] = {"PATH": bindir}
    for head, binary in (("vendor-cli", "vendor-cli"), ("claude-json-stargate", "claude")):
        cfg["agents"]["noop"]["command"] = [head, "--print"]
        config.write_text(yaml.safe_dump(cfg))
        proc = doctor(repo, config, env={"PATH": doctor_path})

        assert proc.returncode == 0, proc.stdout
        for required in (head, binary):
            assert any(
                line.split()[:2] == ["FOUND", required]
                for line in proc.stdout.splitlines()
            ), proc.stdout


def test_a_cli_removed_from_an_agents_environment_is_reported_missing(root: Path) -> None:
    # A working developer must not mask an architect that cannot reach the
    # same CLI, whether it names the CLI directly or uses a known wrapper.
    repo = make_repo(root)
    config = root / "agents.yaml"
    write_config(config, 'echo "VERDICT: APPROVED"', test_command="true")
    cfg = yaml.safe_load(config.read_text())
    bindir = _wrapper_only_path(root, "claude-json-stargate")
    fake_bin(root, "vendor-cli", "claude")
    empty = root / "empty"
    empty.mkdir()
    for head, binary in (
        ("vendor-cli", "vendor-cli"),
        (str(Path(bindir) / "claude-json-stargate"), "claude"),
    ):
        cfg["agents"]["noop"]["command"] = [head, "--print"]
        cfg["agents"]["dev"]["command"] = [binary]
        for search_path in (str(empty), None):
            cfg["agents"]["noop"]["env"] = {"PATH": search_path}
            config.write_text(yaml.safe_dump(cfg))
            proc = doctor(repo, config, env={"PATH": bindir})

            assert proc.returncode == 1, proc.stdout
            lines = [
                line for line in proc.stdout.splitlines()
                if line.split()[:2] == ["MISSING", binary]
            ]
            assert len(lines) == 1, proc.stdout
            assert lines[0].endswith("not on the PATH of: architect"), proc.stdout
            if binary == "claude":
                assert (
                    "configured directly; also required by claude-json-stargate" in lines[0]
                ), proc.stdout
                assert any(
                    line.split()[:2] == ["FOUND", head]
                    for line in proc.stdout.splitlines()
                ), proc.stdout
