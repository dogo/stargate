#!/usr/bin/env python3
from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path
from typing import Any


from .stages import orchestrate
from .config import (
    PROJECT_CONFIG,
    init_config,
    init_prompts,
    load_config,
    resolve_config,
)
from .run import (
    REDOABLE_STAGES,
    clean_runs,
    list_runs,
)
from .doctor import doctor
from .core import (
    StargateError,
    Terminated,
    repo_root,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stargate",
        description="Tiny Claude Code + Codex CLI multi-agent orchestrator.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help=f"Complete standalone config. Without it, ./{PROJECT_CONFIG}, "
        "the user config and packaged defaults are layered.",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    doctor_parser = sub.add_parser(
        "doctor", help="Check local CLI dependencies and configuration."
    )
    doctor_parser.add_argument(
        "--probe", action="store_true",
        help="Make one real, potentially billable call to each unique agent.",
    )
    sub.add_parser(
        "init-config",
        help="Copy the packaged agents.yaml to ~/.config/stargate/agents.yaml.",
    )
    sub.add_parser(
        "init-prompts",
        help="Copy the packaged prompts to ~/.config/stargate/prompts/ so they "
        "can be edited without touching the install.",
    )
    sub.add_parser(
        "list", aliases=["runs"],
        help="List the runs recorded in this repository, newest first."
    )
    clean = sub.add_parser(
        "clean", help="Remove a run's merged branch, clean worktree and artifacts."
    )
    clean.add_argument("run_id", nargs="?", help="Run ID shown by 'stargate list'.")
    clean.add_argument(
        "--all", action="store_true", dest="all_runs",
        help="Clean every recorded run that passes the safety checks.",
    )

    run = sub.add_parser("run", help="Plan, implement, review and fix a task.")
    run.add_argument("task", help="Feature/bug/task description.")
    run.add_argument(
        "--base-ref",
        default=None,
        help="Git ref to branch from. Defaults to the current branch/ref.",
    )
    run.add_argument(
        "--name",
        default=None,
        help="Short name for the branch and run id, e.g. --name 'passkey auth'. "
        "Overrides the name the architect suggests.",
    )

    resume = sub.add_parser(
        "resume",
        help="Continue a run that failed partway, reusing its plan, worktree, "
        "config and prompts.",
    )
    resume.add_argument("run_id", help="Run ID, as printed by the original run.")
    resume.add_argument(
        "--redo", action="append", default=[], choices=REDOABLE_STAGES,
        metavar="STAGE",
        help="Run this completed stage again instead of skipping it "
        f"({', '.join(REDOABLE_STAGES)}). Repeatable.",
    )

    for parser_ in (run, resume):
        parser_.add_argument(
            "--no-commit",
            action="store_true",
            help="Leave the run's work uncommitted in the worktree "
            "(overrides settings.commit).",
        )
        parser_.add_argument(
            "--max-review-loops",
            type=int,
            default=None,
            help="Override settings.max_review_loops.",
        )
    return parser


def install_signal_handlers() -> None:
    """Turn catchable termination into the existing resumable failure path."""
    handled = [
        signum for name in ("SIGTERM", "SIGHUP")
        if (signum := getattr(signal, name, None)) is not None
    ]

    def terminate(signum: int, _frame: Any) -> None:
        # One shot lets a second signal terminate even if cleanup gets stuck.
        for handled_signum in handled:
            signal.signal(handled_signum, signal.SIG_DFL)
        raise Terminated(signum)

    for signum in handled:
        signal.signal(signum, terminate)


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "init-config":
        return init_config(script_dir)
    if args.command == "init-prompts":
        return init_prompts(script_dir)

    try:
        if args.command in ("run", "resume"):
            # SIGINT already becomes KeyboardInterrupt; replacing it would add
            # a second path for an interrupt that is already recorded safely.
            install_signal_handlers()

        if args.command in ("list", "runs"):
            return list_runs(repo_root(Path.cwd()))
        if args.command == "clean":
            return clean_runs(repo_root(Path.cwd()), args.run_id, args.all_runs)

        config_paths = resolve_config(args.config, script_dir)
        config, layers = load_config(config_paths)
        if args.command == "doctor":
            return doctor(
                config,
                layers,
                script_dir,
                probe=args.probe,
                explicit_config=args.config is not None,
            )
        if args.command in ("run", "resume"):
            return orchestrate(args, script_dir, config)
        parser.error("Unknown command")
        return 2
    except StargateError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt as exc:
        signum = getattr(exc, "signum", signal.SIGINT)
        message = "Interrupted" if signum == signal.SIGINT else "Terminated"
        print(f"\n{message}.", file=sys.stderr)
        return 128 + int(signum)
