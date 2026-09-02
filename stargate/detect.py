"""Guessing the project's test command from what is checked in, and deciding
whether a guess may run."""
from __future__ import annotations

import contextlib
import json
import re
from pathlib import Path
from typing import Any

from .core import Detected, StargateError

DETECTION_MODES = ("report", "auto", "off")


DETECTION_READ_LIMIT = 65536


# A detected command is input the user never wrote. It may run unattended after
# every fixer, influence the review verdict and set the process exit code; a bad
# guess can also be a deploy-ish Make target or a watcher that only ends at the
# timeout. The default therefore fixes the dangerous silence by reporting the
# guess and its evidence, without executing it. Users who accept that risk can
# opt into `auto`; `off` records that the repository intentionally has no test.
def detection_mode(config: dict[str, Any]) -> str:
    settings = config.get("settings", {})
    mode = str(settings.get("test_command_detection", "report") or "report")
    mode = mode.strip().lower()
    if mode not in DETECTION_MODES:
        raise StargateError(
            "settings.test_command_detection must be one of report, auto, off"
        )
    return mode


def read_detection_file(path: Path) -> str | None:
    """Read enough evidence for detection without trusting project file size."""
    try:
        with path.open(encoding="utf-8") as handle:
            return handle.read(DETECTION_READ_LIMIT)
    except (OSError, UnicodeError, ValueError):
        return None


def is_file(path: Path) -> bool:
    with contextlib.suppress(OSError):
        return path.is_file()
    return False


def detect_test_commands(root: Path) -> list[Detected]:
    """Return all likely commands in a deliberate, user-visible priority."""
    detected: list[Detected] = []

    # A hand-written project entry point commonly wraps the native runner, so
    # it outranks language metadata. All lower-ranked matches are still shown.
    for name in ("Makefile", "makefile", "GNUmakefile"):
        text = read_detection_file(root / name)
        if text is not None and re.search(r"(?m)^test\s*:(?!=)", text):
            detected.append(Detected("make test", f"{name}: test target"))
            break

    package = read_detection_file(root / "package.json")
    if package is not None:
        with contextlib.suppress(json.JSONDecodeError, TypeError, AttributeError):
            script = json.loads(package).get("scripts", {}).get("test", "")
            script = script.strip() if isinstance(script, str) else ""
            if script and "error: no test specified" not in script.lower():
                runner = (
                    "pnpm" if is_file(root / "pnpm-lock.yaml")
                    else "yarn" if is_file(root / "yarn.lock")
                    else "npm"
                )
                detected.append(Detected(
                    f"{runner} test", "package.json: scripts.test"
                ))

    for filename, command in (
        ("Cargo.toml", "cargo test"),
        ("go.mod", "go test ./..."),
        ("Package.swift", "swift test"),
    ):
        if is_file(root / filename):
            detected.append(Detected(command, filename))

    pytest_source = ""
    pyproject = read_detection_file(root / "pyproject.toml")
    if pyproject is not None:
        if re.search(r"(?im)^\s*\[tool\.pytest(?:\.|\])", pyproject):
            pytest_source = "pyproject.toml: tool.pytest"
        elif (
            re.search(r"(?im)^\s*pytest(?:[-_.][\w.-]+)?\s*=", pyproject)
            or re.search(
                r'''(?i)["']pytest(?:[-_.][\w.-]+)?(?:[<>=~!][^"']*)?["']''',
                pyproject,
            )
        ):
            pytest_source = "pyproject.toml: pytest dependency"
    if not pytest_source and is_file(root / "pytest.ini"):
        pytest_source = "pytest.ini"
    if not pytest_source:
        for filename in ("setup.cfg", "tox.ini"):
            text = read_detection_file(root / filename)
            if text is not None and re.search(r"(?im)^\s*\[tool:pytest\]", text):
                pytest_source = f"{filename}: tool:pytest"
                break
    if not pytest_source:
        tests = root / "tests"
        with contextlib.suppress(OSError):
            if tests.is_dir() and any(path.is_file() for path in tests.rglob("test_*.py")):
                pytest_source = "tests/: test_*.py"
    # A root-level test_*.py is not enough evidence: this repository's smoke
    # test takes a custom argument and pytest collection would fail. That false
    # positive is exactly why detection reports rather than runs by default.
    if pytest_source:
        detected.append(Detected("pytest -q", pytest_source))

    return detected


def selected_test_command(
    config: dict[str, Any], root: Path
) -> tuple[str, list[Detected]]:
    """The command stargate would run, plus every detected candidate.

    Report-only detection intentionally grants nothing: its candidate is input
    the user has not approved, so letting an agent execute it would defeat the
    mode even if the orchestrator itself abstained.
    """
    settings = config.get("settings", {})
    configured = str(settings.get("test_command", "") or "").strip()
    if configured:
        return configured, []
    mode = detection_mode(config)
    if mode == "off":
        return "", []
    detected = detect_test_commands(root)
    if mode == "auto" and detected:
        return detected[0].command, detected
    return "", detected
