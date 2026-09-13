"""The shared runner's deadline, which every agent and test command depends on."""

from __future__ import annotations

import time
from pathlib import Path

from stargate.core import HEARTBEAT_SECONDS, StargateError, run_process


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
