"""The opencode wrapper's failure signalling, output filtering and surviving processes."""
from __future__ import annotations

import os
import signal as signals
import subprocess
import time
from contextlib import suppress
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
    return proc, output.read_text() if output.is_file() else ""


def test_an_unwritable_answer_cannot_turn_a_successful_request_into_success(root: Path) -> None:
    for status in (0, 17):
        case = root / str(status)
        case.mkdir()
        # A directory fails writes even when the suite runs as root.
        (case / "answer.txt").mkdir()
        proc, _ = run_wrapper(case, f'echo reply; exit {status}')
        assert proc.returncode != 0, proc.stdout + proc.stderr
        if status:
            assert proc.returncode == status, proc.stdout + proc.stderr


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


def test_signalling_the_wrapper_leaves_no_opencode_still_running_and_billing(root: Path) -> None:
    _assert_signalled_request_stops(root, new_session=True)


def test_signalling_a_nonleader_wrapper_stops_its_direct_opencode_request(root: Path) -> None:
    _assert_signalled_request_stops(root, new_session=False)


def _assert_signalled_request_stops(root: Path, *, new_session: bool) -> None:
    for signum, status in ((signals.SIGINT, 130), (signals.SIGTERM, 143), (signals.SIGHUP, 129)):
        case = root / str(signum)
        case.mkdir()
        bindir = Path(fake_bin(case, "opencode"))
        script = (
            'sleep 30 &\nprintf "%s\\n%s\\n" "$$" "$!" >"$PIDS"\nwait\n'
            if new_session else 'echo "$$" >"$PIDS"\nexec sleep 30\n'
        )
        (bindir / "opencode").write_text("#!/bin/sh\n" + script)
        expected_pids = 2 if new_session else 1
        temporary = case / "tmp"
        temporary.mkdir()
        pidfile = case / "pids"
        with (case / "trace").open("w+") as trace:
            child = subprocess.Popen(
                [str(ROOT / "examples/opencode/opencode-stargate"), str(case / "answer"), "p"],
                env={"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", "HOME": str(case),
                     "TMPDIR": str(temporary), "PIDS": str(pidfile)},
                stdin=subprocess.DEVNULL, stdout=trace, stderr=subprocess.STDOUT,
                start_new_session=new_session,
            )
            pids = []
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if pidfile.exists():
                        pids = [int(pid) for pid in pidfile.read_text().splitlines()]
                        if len(pids) == expected_pids:
                            break
                    time.sleep(0.05)
                # Signal only the wrapper: signalling the group would hide the bug.
                child.send_signal(signum)
                child.wait(timeout=10)
                trace.seek(0)
                proc = subprocess.CompletedProcess(child.args, child.returncode, trace.read())
                assert len(pids) == expected_pids, proc.stdout
                assert proc.returncode == status, proc.stdout
                assert not list(temporary.iterdir()), proc.stdout
                remaining = set(pids)
                deadline = time.monotonic() + 5
                while remaining and time.monotonic() < deadline:
                    for pid in tuple(remaining):
                        try:
                            os.kill(pid, 0)
                        except ProcessLookupError:
                            remaining.remove(pid)
                            continue
                        # A zombie cannot run or bill; PID 1 may delay reaping it.
                        try:
                            stat = Path(f"/proc/{pid}/stat").read_text()
                        except FileNotFoundError:
                            continue
                        if stat.rsplit(")", 1)[1].split()[0] == "Z":
                            remaining.remove(pid)
                    if remaining:
                        time.sleep(0.05)
                assert not remaining, proc.stdout + f"\nsurviving opencode pids: {remaining}"
            finally:
                # Also clean up the filter if the regression fails before wait returns.
                if new_session:
                    with suppress(ProcessLookupError):
                        os.killpg(child.pid, signals.SIGKILL)
                else:
                    for pid in pids:
                        with suppress(ProcessLookupError):
                            os.kill(pid, signals.SIGKILL)
                    if child.poll() is None:
                        child.kill()
                child.wait(timeout=10)
