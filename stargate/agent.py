"""Running one agent for one role: retries, the token budget it spends, and
the fingerprint that tells a repeated failure from a new one."""
from __future__ import annotations

import re
import shlex
import time
from pathlib import Path

from .config import (
    agent_command,
    agent_entry,
    agent_env,
    expand_test_command,
    parse_usage,
    retry_settings,
    token_cap,
)
from .core import RunContext, StargateError, run_process, termination_requested

FINGERPRINT_LINES = 20


def record_usage(ctx: RunContext, role: str, transcript: str) -> None:
    """Charge one completed attempt, including one the vendor rejected late."""
    used = parse_usage(
        transcript, agent_entry(ctx.config, role).get("usage_pattern")
    )
    ctx.tokens_used += used
    if used:
        cap = token_cap(ctx.config)
        budget = f" of {cap:,}" if cap else ""
        print(
            f"\n[{role}] reported {used:,} tokens; "
            f"{ctx.tokens_used:,}{budget} used so far."
        )


def attempt_log_path(output_path: Path, attempt: int) -> Path:
    # Attempt one keeps the historical path, so retries-off runs retain the
    # same artifacts while later failures cannot overwrite its evidence.
    suffix = "" if attempt == 1 else f".attempt-{attempt}"
    return output_path.with_name(output_path.name + suffix + ".log")


def failure_fingerprint(error: StargateError, trace: str) -> tuple[str, str]:
    """Normalize per-attempt noise without interpreting vendor error text."""
    # The prefix is ours and distinguishes timeout from a non-zero exit. The
    # rest includes attempt-specific trace paths and the full command, neither
    # of which says whether another attempt has a chance of succeeding.
    reason = str(error).split(" (", 1)[0]
    tail = "\n".join(trace.strip().splitlines()[-FINGERPRINT_LINES:])
    return re.sub(r"\d+", "#", reason), re.sub(r"\d+", "#", tail)


def invoke_agent(
    ctx: RunContext,
    role: str,
    prompt: str,
    cwd: Path,
    output_path: Path,
) -> str:
    """Run one agent and return its FINAL MESSAGE, not its stdout.

    The distinction matters: `codex exec` streams the whole session — reasoning,
    every command it ran, a token footer — to stdout. Forwarding that as {plan}
    or {review} makes each hop pay for the previous hop's trace. An agent whose
    command contains "{output}" is handed a file path to write its last message
    to, and that file is what gets forwarded; its stdout is kept as a .log.

    Retries stay inside this single invocation so no completed role is replayed.
    Each attempt has its own trace, both for debugging and to count any usage
    the failed process reported exactly once.
    """
    cmd = agent_command(ctx.config, role)
    writes_final = any("{output}" in part for part in cmd)
    # Expand {output} first so those literal characters inside a configured
    # test command cannot unexpectedly become a path.
    cmd = [part.replace("{output}", str(output_path)) for part in cmd]
    cmd = expand_test_command(cmd, ctx.test_command)
    timeout = float(ctx.config.get("settings", {}).get("agent_timeout_seconds", 1800))
    env = agent_env(agent_entry(ctx.config, role))
    retries, backoff = retry_settings(ctx.config)
    attempts = retries + 1
    previous_failure: tuple[str, str] | None = None

    for attempt in range(1, attempts + 1):
        log_path = attempt_log_path(output_path, attempt)
        print(f"trace: tail -f {shlex.quote(str(log_path))}", flush=True)

        # A retry or explicitly redone stage must not inherit an earlier
        # answer and pass the output contract after writing nothing.
        if writes_final and output_path.exists():
            output_path.unlink()

        started = time.monotonic()
        try:
            proc = run_process(
                [*cmd, prompt], cwd, timeout=timeout or None, log_path=log_path,
                env=env,
            )
        except OSError as exc:
            # A process that cannot be started will fail the same way after a
            # backoff; unlike an agent exit, it never made a remote request.
            raise StargateError(
                f"Could not start the agent for role '{role}': {exc}"
            ) from exc
        except StargateError as exc:
            if termination_requested():
                raise
            if retries:
                trace = log_path.read_text() if log_path.exists() else ""
                record_usage(ctx, role, trace)
                print(
                    f"\n[{role}] attempt {attempt} of {attempts} failed: {exc}",
                    flush=True,
                )
            else:
                # Keeping retries disabled must preserve the original failure
                # path, including terminal output and token accounting.
                raise

            if attempt == attempts:
                raise

            fingerprint = failure_fingerprint(exc, trace)
            if fingerprint == previous_failure:
                remaining = attempts - attempt
                print(
                    f"[{role}] failed identically twice; not retrying "
                    f"{remaining} more time(s).",
                    flush=True,
                )
                raise
            previous_failure = fingerprint

            wait = backoff * 2 ** (attempt - 1)
            print(
                f"[{role}] retrying in {wait:g}s "
                f"(attempt {attempt + 1} of {attempts}).",
                flush=True,
            )
            time.sleep(wait)
            continue

        transcript = proc.stdout or ""
        print(
            f"\n[{role}] exit {proc.returncode} in "
            f"{time.monotonic() - started:.0f}s",
            flush=True,
        )
        record_usage(ctx, role, transcript)
        break

    if not writes_final:
        output_path.write_text(transcript)
        return transcript

    final = output_path.read_text() if output_path.exists() else ""
    if not final.strip():
        raise StargateError(
            f"Agent for role '{role}' declares {{output}} but wrote nothing to "
            f"{output_path}. Check that its CLI supports the flag you passed."
        )
    return final
