"""The shared runner's deadline, which every agent and test command depends on."""

from __future__ import annotations

import os
import shlex
import signal
import time
from contextlib import suppress
from pathlib import Path

import yaml

from stargate.core import HEARTBEAT_SECONDS, StargateError, run_process
from tests.harness import doctor, fake_bin, make_repo


def test_a_timeout_shorter_than_one_heartbeat_is_still_enforced_on_time(
    root: Path,
) -> None:
    """Found by a test that failed intermittently four times before anyone looked.

    The wait was a fixed heartbeat interval, so a deadline shorter than one
    heartbeat could not be enforced until a whole interval had passed -- and a
    process that exited inside that window broke out of the loop before the
    deadline was consulted at all. Whether the kill happened was a race at the
    heartbeat boundary, which is why it only showed up under load.

    Any agent_timeout_seconds below HEARTBEAT_SECONDS was silently not honored.
    """
    started = time.monotonic()

    try:
        run_process(
            ["/bin/sh", "-c", f"sleep {HEARTBEAT_SECONDS * 2}"],
            root,
            timeout=1,
            log_path=root / "slow.log",
        )
    except StargateError as exc:
        elapsed = time.monotonic() - started
        assert "timed out after 1s" in str(exc), str(exc)
        # Generous, and still far below the heartbeat this used to wait for.
        assert elapsed < HEARTBEAT_SECONDS / 2, f"enforced only after {elapsed:.1f}s"
    else:
        raise AssertionError("a process past its deadline must not be reported as success")


def test_a_process_that_finishes_early_is_not_reported_as_timed_out(root: Path) -> None:
    """The clamp must not turn a normal exit into a deadline breach."""
    proc = run_process(
        ["/bin/sh", "-c", "echo done"], root, timeout=HEARTBEAT_SECONDS,
        log_path=root / "quick.log",
    )

    assert proc.returncode == 0, proc
    assert "done" in proc.stdout, proc.stdout


def test_a_timed_out_probe_leaves_no_agent_process_still_running(root: Path) -> None:
    repo = make_repo(root)
    bindir = fake_bin(root, "hanging-agent")
    pidfile = root / "child.pid"
    temporary_pidfile = root / "child.pid.tmp"
    executable = Path(bindir) / "hanging-agent"
    executable.write_text(
        "#!/bin/sh\nsleep 600 >/dev/null 2>&1 &\n"
        f"echo $! > {shlex.quote(str(temporary_pidfile))}\n"
        f"mv {shlex.quote(str(temporary_pidfile))} {shlex.quote(str(pidfile))}\n"
        "wait\n"
    )
    config = root / "agents.yaml"
    config.write_text(yaml.safe_dump({
        "agents": {"hanging": {"command": ["hanging-agent"], "probe": "cheap"}},
        "workflow": dict.fromkeys(("architect", "developer", "reviewer", "fixer"), "hanging"),
        "settings": {"probe_timeout_seconds": 2, "agent_timeout_seconds": 60},
    }))
    pid = None
    started = time.monotonic()
    try:
        proc = doctor(
            repo, config, "--probe",
            env={"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", "TMPDIR": str(root)},
        )
        elapsed = time.monotonic() - started
        assert pidfile.exists(), proc.stdout
        pid = int(pidfile.read_text())
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "probe timed out after 2.0s" in proc.stdout, proc.stdout
        assert elapsed < 15, proc.stdout + f"\nprobe took {elapsed:.1f}s"

        # Zombies cannot run or bill; PID 1 may take time to reap an orphan.
        alive = True
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                alive = False
                break
            try:
                state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
            except FileNotFoundError:
                pass
            else:
                if state == "Z":
                    alive = False
                    break
            time.sleep(0.05)
        assert not alive, proc.stdout + f"\nsurviving child: {pid}"
    finally:
        # A failing regression must not leave the fake paid request running.
        if pid is None and pidfile.exists():
            pid = int(pidfile.read_text())
        if pid is not None:
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
