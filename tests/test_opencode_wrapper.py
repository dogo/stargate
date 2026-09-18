"""The opencode wrapper's failure signalling and what its filter is allowed to delete."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from tests.harness import ROOT, fake_bin


def run_wrapper(root: Path, script: str) -> tuple[subprocess.CompletedProcess[str], str]:
    bindir = Path(fake_bin(root, "opencode"))
    (bindir / "opencode").write_text("#!/bin/sh\n" + script + "\n")
    temporary = root / "tmp"
    temporary.mkdir()
    output = root / "answer.txt"
    proc = subprocess.run(
        [str(ROOT / "examples/opencode/opencode-stargate"), str(output), "--agent", "plan", "p"],
        env={"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", "HOME": str(root),
             "TMPDIR": str(temporary)},
        text=True, capture_output=True, timeout=60,
    )
    assert not list(temporary.iterdir()), proc.stdout + proc.stderr
    return proc, output.read_text()


def test_a_failing_opencode_exits_nonzero_instead_of_answering_with_the_error(root: Path) -> None:
    proc, answer = run_wrapper(root, 'echo "authentication failed" >&2; exit 17')
    assert proc.returncode == 17, proc.stdout + proc.stderr
    assert "authentication failed" in answer, proc.stdout + proc.stderr


def test_a_blockquote_and_a_bullet_in_the_reply_survive_the_filter(root: Path) -> None:
    proc, answer = run_wrapper(root, """cat <<'EOF'

> plan · google/gemini-3.6-flash
→ Read AGENTS.md
← Write impl.txt
✱ Glob *.py

> quoted text from the diff
• a bullet
✓ checked
● another bullet
⋮ ordinary prose
◆ a future marker
\x1b[32mVERDICT: APPROVED\x1b[0m
EOF""")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert answer == (
        "> quoted text from the diff\n• a bullet\n✓ checked\n● another bullet\n"
        "⋮ ordinary prose\n◆ a future marker\nVERDICT: APPROVED\n"
    ), proc.stdout + proc.stderr


def test_a_reply_starting_with_a_blockquote_is_not_mistaken_for_a_header(root: Path) -> None:
    proc, answer = run_wrapper(root, 'echo "> quoted text"; echo done')
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert answer == "> quoted text\ndone\n", proc.stdout + proc.stderr


def test_opencode_is_still_handed_a_pipe_and_not_a_regular_file(root: Path) -> None:
    proc, answer = run_wrapper(root, '[ -p /dev/stdout ] || echo REGULAR-FILE; echo done')
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert answer == "done\n", proc.stdout + proc.stderr


def test_interrupting_the_wrapper_removes_its_private_fifo_directory(root: Path) -> None:
    for signal, status in (("INT", 130), ("TERM", 143), ("HUP", 129)):
        case = root / signal
        case.mkdir()
        proc, _ = run_wrapper(case, f'kill -{signal} "$PPID"; echo done')
        assert proc.returncode == status, proc.stdout + proc.stderr
