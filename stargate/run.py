"""The lifecycle of one run: picking its base commit, reserving its id and
branch, its worktree, the state file that makes it resumable, and cleanup."""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from .config import ROLES, find_prompt, token_cap
from .core import (
    RunContext,
    StargateError,
    git,
    git_quiet,
    short_name,
    slugify,
)


def resolve_base_ref(repo: Path, requested: str | None) -> tuple[str, str]:
    if requested:
        ref = requested
    else:
        proc = git(repo, "rev-parse", "--abbrev-ref", "HEAD", capture=True)
        ref = proc.stdout.strip()
        if ref == "HEAD":
            ref = git(repo, "rev-parse", "HEAD").stdout.strip()

    commit = git(
        repo, "rev-parse", "--verify", "--end-of-options",
        f"{ref}^{{commit}}", capture=True,
    ).stdout.strip()

    symbolic = subprocess.run(
        ["git", "rev-parse", "--symbolic-full-name", "--verify",
         "--end-of-options", ref],
        cwd=str(repo), text=True, capture_output=True,
    )
    local_branch = symbolic.stdout.strip()
    if symbolic.returncode == 0 and local_branch.startswith("refs/heads/"):
        branch = local_branch.removeprefix("refs/heads/")
        remote = subprocess.run(
            ["git", "config", "--get", f"branch.{branch}.remote"],
            cwd=str(repo), text=True, capture_output=True,
        ).stdout.strip()
        merge_ref = subprocess.run(
            ["git", "config", "--get", f"branch.{branch}.merge"],
            cwd=str(repo), text=True, capture_output=True,
        ).stdout.strip()
        if remote and merge_ref:
            upstream = (
                merge_ref if remote == "."
                else f"{remote}/{merge_ref.removeprefix('refs/heads/')}"
            )
            if remote == ".":
                upstream_proc = subprocess.run(
                    ["git", "rev-parse", "--verify", "--end-of-options",
                     f"{merge_ref}^{{commit}}"],
                    cwd=str(repo), text=True, capture_output=True,
                )
            else:
                try:
                    upstream_proc = subprocess.run(
                        ["git", "ls-remote", "--exit-code", "--refs",
                         remote, merge_ref],
                        cwd=str(repo), text=True, capture_output=True,
                        stdin=subprocess.DEVNULL, timeout=60,
                        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                    )
                except subprocess.TimeoutExpired as exc:
                    raise StargateError(
                        f"Timed out validating upstream {upstream!r}."
                    ) from exc
            if upstream_proc.returncode != 0 or not upstream_proc.stdout.strip():
                detail = upstream_proc.stderr.strip() or "ref not found"
                raise StargateError(
                    f"Could not validate upstream {upstream!r}: {detail}"
                )
            upstream_commit = upstream_proc.stdout.split()[0]
            if upstream_commit != commit:
                contains_upstream = subprocess.run(
                    ["git", "merge-base", "--is-ancestor", upstream_commit,
                     commit],
                    cwd=str(repo), stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                ).returncode == 0
                if not contains_upstream:
                    raise StargateError(
                        f"Base branch {ref!r} is behind or diverged from "
                        f"upstream {upstream!r}. Update it before running "
                        "Stargate."
                    )
    return ref, commit


def warn_if_dirty(repo: Path) -> None:
    status = git(repo, "status", "--porcelain").stdout.strip()
    if status:
        print(
            "\nWarning: the source repository has local changes. "
            "The agent worktree is created from the selected base ref, "
            "so those uncommitted source-tree changes are NOT copied.",
            file=sys.stderr,
        )


def default_worktree_root(repo: Path) -> Path:
    return repo.parent / ".stargate-worktrees" / repo.name


def branch_exists(repo: Path, branch: str) -> bool:
    proc = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=str(repo), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return proc.returncode == 0


def unique_branch(repo: Path, branch: str) -> str:
    for attempt in range(1, 100):
        candidate = branch if attempt == 1 else f"{branch}-{attempt}"
        if not branch_exists(repo, candidate):
            return candidate
    raise StargateError(f"Could not find an unused branch name based on {branch!r}.")


def reserve_run(repo: Path, now: str, slug: str) -> tuple[str, str, Path]:
    """Reserve one discriminator for both artifacts and the initial branch.

    A second process in the same second would otherwise share the artifacts
    directory while Git also refuses to attach its branch to another worktree.
    Creating the directory is the atomic claim; old first-attempt names retain
    their exact shape, so `runs` and `resume` need no format migration.
    """
    for attempt in range(1, 100):
        suffix = "" if attempt == 1 else f"-{attempt}"
        tag = f"{now}{suffix}"
        run_id = f"{now}-{slug}{suffix}"
        branch = f"stargate/{slug}-{tag}"
        if branch_exists(repo, branch):
            continue
        artifacts = repo / ".stargate" / "runs" / run_id
        try:
            artifacts.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            continue
        return run_id, branch, artifacts
    raise StargateError(
        f"Could not reserve a unique run name for {slug!r} at {now}."
    )


def make_context(
    repo: Path,
    config: dict[str, Any],
    task: str,
    base_ref: str | None,
    name: str | None = None,
) -> RunContext:
    now = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    named = short_name(name or "") if name is not None else ""
    if name is not None and not named:
        print(
            "Warning: --name produced no usable slug; using the task text.",
            file=sys.stderr,
        )
    slug = named or slugify(task)
    base, base_commit = resolve_base_ref(repo, base_ref)
    run_id, branch, artifacts = reserve_run(repo, now, slug)
    tag = branch.removeprefix(f"stargate/{slug}-")

    configured = str(config.get("settings", {}).get("worktree_root", "") or "").strip()
    worktree_root = Path(os.path.expanduser(configured)).resolve() if configured else default_worktree_root(repo)
    worktree = worktree_root / run_id
    # Keep run artifacts out of the target repo's git status without touching
    # the user's own .gitignore.
    (repo / ".stargate" / ".gitignore").write_text("*\n")
    worktree.parent.mkdir(parents=True, exist_ok=True)

    return RunContext(
        repo=repo,
        config=config,
        run_id=run_id,
        slug=slug,
        branch=branch,
        base_ref=base,
        base_commit=base_commit,
        worktree=worktree,
        artifacts=artifacts,
        task=task,
        tag=tag,
        named_by_user=bool(named),
    )


def budget_spent(ctx: RunContext, next_phase: str) -> bool:
    """Whether the cap is reached. Checked BETWEEN phases: nothing here can stop
    an agent already running, so a single runaway invocation still overshoots."""
    cap = token_cap(ctx.config)
    if not cap or ctx.tokens_used < cap:
        return False
    print(
        f"\nToken budget reached: {ctx.tokens_used:,} of {cap:,} used. "
        f"Stopping before {next_phase}.",
        file=sys.stderr,
    )
    return True


STAGES = ("architect", "worktree", "developer", "review")


# These are the completed records that skip work. The worktree is reused
# regardless, and the review loop already restarts from its first attempt.
REDOABLE_STAGES = ("architect", "developer")


def save_state(ctx: RunContext, status: str, error: str | None = None) -> None:
    """Record where the run got to, so a failed stage can be resumed instead of
    restarting the whole flow and leaving a second plan, branch and worktree."""
    path = ctx.artifacts / "state.json"
    started = json.loads(path.read_text()).get("started_at") if path.exists() else None
    path.write_text(json.dumps({
        "run_id": ctx.run_id,
        "task": ctx.task,
        "repo": str(ctx.repo),
        "base_ref": ctx.base_ref,
        "base_commit": ctx.base_commit,
        "branch": ctx.branch,
        "worktree": str(ctx.worktree),
        "stage": ctx.stage,
        "status": status,
        "error": error,
        "completed": sorted(ctx.done),
        "tokens_used": ctx.tokens_used,
        "named_by_user": ctx.named_by_user,
        "test_artifacts": sorted(ctx.test_artifacts),
        "commit": ctx.commit or None,
        "commit_error": ctx.commit_error or None,
        "started_at": started or dt.datetime.now().isoformat(timespec="seconds"),
        "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
    }, indent=2) + "\n")


def enter_stage(ctx: RunContext, stage: str) -> None:
    ctx.stage = stage
    save_state(ctx, "running")


def complete_stage(ctx: RunContext, stage: str) -> None:
    ctx.done.add(stage)
    save_state(ctx, "running")


def load_run(repo: Path, run_id: str, config: dict[str, Any], use_frozen: bool) -> RunContext:
    artifacts = repo / ".stargate" / "runs" / run_id
    state_path = artifacts / "state.json"
    if not state_path.exists():
        raise StargateError(f"No run state at {state_path}")
    state = json.loads(state_path.read_text())
    frozen = artifacts / "config.yaml"
    if use_frozen and frozen.exists():
        # Default to the run's own frozen config so resuming does not silently
        # change the agents the earlier stages ran under. An explicit --config
        # overrides it, which is how you resume past a bad agent definition.
        config = yaml.safe_load(frozen.read_text()) or config
    return RunContext(
        repo=repo,
        config=config,
        run_id=state["run_id"],
        slug=slugify(state["task"]),
        branch=state["branch"],
        base_ref=state["base_ref"],
        base_commit=str(state.get("base_commit") or state["base_ref"]),
        worktree=Path(state["worktree"]),
        artifacts=artifacts,
        task=state["task"],
        stage=state.get("stage", "init"),
        done=set(state.get("completed", [])),
        tokens_used=int(state.get("tokens_used", 0)),
        test_artifacts=set(state.get("test_artifacts", [])),
        commit=str(state.get("commit") or ""),
        commit_error=str(state.get("commit_error") or ""),
        tag=(
            match.group(1)
            if (match := re.search(
                r"-(\d{8}-\d{6}(?:-\d+)?)$", str(state["branch"])
            )) else ""
        ),
        named_by_user=bool(state.get("named_by_user", False)),
    )


RUN_TASK_WIDTH = 60


RESUMABLE_STATUSES = ("running", "failed")


def read_run(path: Path) -> dict[str, Any]:
    """Build one listing row, including runs whose state cannot be read."""
    state_path = path / "state.json"
    row: dict[str, Any] = {
        "run_id": path.name,
        "status": "unknown",
        "stage": "-",
        "branch": "(unknown)",
        "worktree": "(unknown)",
        "worktree_missing": False,
        "task": "-",
        "updated": "-",
        "resumable": False,
        "error": "",
    }
    try:
        state = json.loads(state_path.read_text())
        if not isinstance(state, dict):
            raise ValueError("state.json is not an object")
    except (OSError, ValueError) as exc:
        row["error"] = f"unreadable state.json ({exc})"
        return row

    status = " ".join(str(state.get("status") or "unknown").split())
    worktree = str(state.get("worktree") or "")
    missing = False
    if worktree:
        try:
            missing = not Path(worktree).exists()
        except (OSError, ValueError):
            missing = True
    row.update(
        run_id=" ".join(str(state.get("run_id") or path.name).split()),
        status=status,
        stage=" ".join(str(state.get("stage") or "-").split()),
        branch=" ".join(str(state.get("branch") or "(unknown)").split()),
        worktree=" ".join(worktree.split()) or "(unknown)",
        worktree_missing=missing,
        updated=" ".join(str(state.get("updated_at") or "-").split()),
        # Tasks are often multi-paragraph input; one row should stay one row.
        task=(" ".join(str(state.get("task") or "").split())[:RUN_TASK_WIDTH] or "-"),
        resumable=status.lower() in RESUMABLE_STATUSES,
        error="",
    )
    return row


def list_runs(repo: Path) -> int:
    root = repo / ".stargate" / "runs"
    # Listing a repository that has never run stargate must not create the
    # bookkeeping directory it is meant only to inspect.
    if not root.is_dir():
        print(f"No recorded runs in {root}")
        return 0

    try:
        paths = sorted(
            (path for path in root.iterdir() if path.is_dir()),
            key=lambda path: path.name,
            reverse=True,
        )
    except OSError as exc:
        raise StargateError(
            f"Could not read recorded runs in {root}: {exc}"
        ) from exc
    if not paths:
        print(f"No recorded runs in {root}")
        return 0

    rows = [read_run(path) for path in paths]
    width = max(len(row["run_id"]) for row in rows)
    print(f"Runs in {repo} (newest first):\n")
    print(f"  {'RUN ID':{width}}  {'STATUS':17} {'STAGE':10} {'UPDATED':19} TASK")
    for row in rows:
        marker = "*" if row["resumable"] else " "
        print(
            f"{marker} {row['run_id']:{width}}  {row['status']:17} "
            f"{row['stage']:10} {row['updated']:19} {row['task']}"
        )
        print(f"    branch    {row['branch']}")
        missing = "  (MISSING)" if row["worktree_missing"] else ""
        print(f"    worktree  {row['worktree']}{missing}")
        if row["error"]:
            print(f"    {row['error']}")

    newest = next((row for row in rows if row["resumable"]), None)
    if newest:
        print(
            "\n* resumable. Resume the newest with: "
            f"stargate resume {newest['run_id']}"
        )
    else:
        print("\nNo runs are marked resumable.")
    return 0


def snapshot(ctx: RunContext, dirs: list[Path]) -> list[Path]:
    """Copy the effective config and all four prompts into the run's artifacts,
    and use those copies for the rest of the run.

    A run outlives its own installation: upgrading or reinstalling the package
    mid-run otherwise deletes the prompts out from under the next role. It also
    makes the run reproducible -- the artifacts say exactly what was used.
    """
    (ctx.artifacts / "config.yaml").write_text(yaml.safe_dump(ctx.config, sort_keys=False))
    frozen = ctx.artifacts / "prompts"
    frozen.mkdir(exist_ok=True)
    for role in ROLES:
        (frozen / f"{role}.md").write_text(find_prompt(dirs, role).read_text())
    return [frozen]


def create_worktree(ctx: RunContext) -> None:
    if ctx.worktree.exists():
        print(f"Reusing existing worktree: {ctx.worktree}")
        return
    exists = git(ctx.repo, "rev-parse", "--verify", ctx.branch, check=False).returncode == 0
    args = ["worktree", "add"] + (
        [str(ctx.worktree), ctx.branch] if exists
        else ["-b", ctx.branch, str(ctx.worktree), ctx.base_commit]
    )
    git(ctx.repo, *args, capture=True)


def clean_run(repo: Path, run_id: str) -> None:
    root = repo / ".stargate" / "runs"
    if not run_id or Path(run_id).name != run_id or run_id in (".", ".."):
        raise StargateError(f"Invalid run ID: {run_id!r}")
    artifacts = root / run_id
    if artifacts.is_symlink() or not artifacts.is_dir():
        raise StargateError(f"No recorded run {run_id!r} in {root}")
    try:
        state = json.loads((artifacts / "state.json").read_text())
    except (OSError, ValueError) as exc:
        raise StargateError(f"Cannot clean {run_id}: unreadable state.json ({exc})") from exc
    if not isinstance(state, dict) or state.get("run_id") != run_id:
        raise StargateError(f"Cannot clean {run_id}: state.json has a different run ID")
    recorded_repo = state.get("repo")
    if not isinstance(recorded_repo, str) or Path(recorded_repo).resolve() != repo:
        raise StargateError(f"Cannot clean {run_id}: state.json belongs to another repository")
    branch = state.get("branch")
    worktree_value = state.get("worktree")
    if not isinstance(branch, str) or not branch.startswith("stargate/"):
        raise StargateError(f"Cannot clean {run_id}: invalid Stargate branch")
    if not isinstance(worktree_value, str) or not worktree_value:
        raise StargateError(f"Cannot clean {run_id}: invalid worktree path")
    worktree = Path(worktree_value).resolve()
    branch_present = branch_exists(repo, branch)
    worktree_present = worktree.exists()

    if worktree_present:
        checked_out = git_quiet(worktree, "symbolic-ref", "--short", "HEAD").strip()
        if checked_out != branch:
            raise StargateError(
                f"Cannot clean {run_id}: {worktree} has branch {checked_out!r}, "
                f"not {branch!r}"
            )
    elif not branch_present:
        shutil.rmtree(artifacts)
        print(f"Cleaned {run_id}: artifacts removed; worktree and branch were absent.")
        return

    if branch_present:
        merged = subprocess.run(
            ["git", "merge-base", "--is-ancestor", f"refs/heads/{branch}", "HEAD"],
            cwd=str(repo), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if merged.returncode == 1:
            raise StargateError(
                f"Cannot clean {run_id}: branch {branch!r} is not merged into HEAD"
            )
        if merged.returncode != 0:
            raise StargateError(f"Cannot determine whether branch {branch!r} is merged")

    if worktree_present:
        git(repo, "worktree", "remove", str(worktree))
    else:
        git(repo, "worktree", "prune", "--expire", "now")
    if branch_present:
        git(repo, "branch", "-d", "--", branch)
    shutil.rmtree(artifacts)
    print(f"Cleaned {run_id}: worktree, branch and artifacts removed.")


def clean_runs(repo: Path, run_id: str | None, all_runs: bool) -> int:
    if all_runs == (run_id is not None):
        raise StargateError("Use either 'stargate clean <run-id>' or 'stargate clean --all'.")
    if run_id is not None:
        clean_run(repo, run_id)
        return 0

    root = repo / ".stargate" / "runs"
    if not root.is_dir():
        print(f"No recorded runs in {root}")
        return 0
    run_ids = sorted(
        (path.name for path in root.iterdir() if path.is_dir()), reverse=True
    )
    if not run_ids:
        print(f"No recorded runs in {root}")
        return 0
    failures: list[tuple[str, str]] = []
    for candidate in run_ids:
        try:
            clean_run(repo, candidate)
        except StargateError as exc:
            failures.append((candidate, str(exc)))
    if failures:
        for candidate, error in failures:
            print(f"  {candidate}: {error}", file=sys.stderr)
        raise StargateError(f"Could not clean {len(failures)} of {len(run_ids)} runs.")
    return 0


def worktree_fingerprint(ctx: RunContext) -> str:
    """Digest the tracked and untracked state that an agent can change."""
    parts = [git_quiet(ctx.worktree, "diff", ctx.base_commit)]
    untracked = git_quiet(
        ctx.worktree, "ls-files", "--others", "--exclude-standard", "-z"
    )
    for name in untracked.split("\0"):
        if not name:
            continue
        # New files are implementation work too. Metadata errs toward allowing
        # a review rather than hashing an arbitrarily large untracked artifact.
        with contextlib.suppress(OSError):
            info = (ctx.worktree / name).lstat()
            parts.append(f"{name}\t{info.st_size}\t{info.st_mtime_ns}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def untracked_entries(worktree: Path) -> set[str]:
    """Return untracked paths at Git's directory-grouped granularity."""
    out = git_quiet(
        worktree,
        "ls-files",
        "--others",
        "--exclude-standard",
        "--directory",
        "--no-empty-directory",
        "-z",
    )
    return {name for name in out.split("\0") if name}
