"""Read a task through the configured commands for its URL's host."""
from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .config import agent_env, task_sources
from .core import StargateError

# ponytail: fixed timeout; make configurable if sources need longer fetches.
FETCH_TIMEOUT_SECONDS = 120


def fetch_task(config: dict[str, Any], url: str, cwd: Path) -> str:
    sources = task_sources(config)
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
    except ValueError as exc:
        raise StargateError(f"--from needs an absolute URL with a host; got {url!r}.") from exc
    if not parsed.scheme or not host:
        raise StargateError(f"--from needs an absolute URL with a host; got {url!r}.")
    source = next((source for source in sources if host.lower() in source["hosts"]), None)
    if source is None:
        raise StargateError(
            f"No task source is configured for host {host!r}; add it to task_sources "
            "in your config. Stargate does not fetch URLs by itself."
        )
    values = {"url": url, "host": host, "path": parsed.path}
    env = agent_env(source)
    failures = []
    for command in source["commands"]:
        # One pass keeps placeholder-looking text in the URL literal.
        cmd = [re.sub(r"\{(url|host|path)\}", lambda m: values[m[1]], part) for part in command]
        try:
            proc = subprocess.run(
                cmd, cwd=cwd, text=True, capture_output=True,
                stdin=subprocess.DEVNULL, timeout=FETCH_TIMEOUT_SECONDS, env=env,
            )
        except subprocess.TimeoutExpired as exc:
            stderr = exc.stderr or b""
            if isinstance(stderr, bytes):
                stderr = stderr.decode(errors="replace")
            reason = f"timed out after {FETCH_TIMEOUT_SECONDS}s: {stderr.strip() or '(no stderr)'}"
        except (OSError, ValueError) as exc:
            reason = str(exc)
        else:
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout
            reason = (
                "exited 0 but produced no text (empty or whitespace-only output is a failed fetch)"
                if proc.returncode == 0 else f"exit {proc.returncode}"
            )
            reason += f": {proc.stderr.strip() or '(no stderr)'}"
        failures.append(f"  $ {shlex.join(cmd)}\n      {reason}")
    raise StargateError(f"Could not read task from {url!r}:\n" + "\n".join(failures))


def branch_hint(url: str) -> str:
    """Use only the last non-empty path segment, without interpreting the ref."""
    segments = [part for part in urlsplit(url).path.split("/") if part]
    return segments[-1] if segments else ""
