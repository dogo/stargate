# AGENTS.md

Guidance for Claude Code, Codex and other agents working in this repository.
Single source: `CLAUDE.md` only imports this file.

## Rules: the template of 12

They apply to every task in this project unless explicitly overridden.
Bias: caution over speed on non-trivial work.

## Rule 1: Think Before Coding
State assumptions explicitly. Ask rather than guess.
Push back when a simpler approach exists. Stop when confused.

## Rule 2: Simplicity First
Minimum code that solves the problem. Nothing speculative.
No abstractions for single-use code.

## Rule 3: Surgical Changes
Touch only what you must. Don't improve adjacent code.
Match existing style. Don't refactor what isn't broken.

## Rule 4: Goal-Driven Execution
Define success criteria. Loop until verified.
Strong success criteria let the agent loop independently.

## Rule 5: Use the model only for judgment calls
Use for: classification, drafting, summarization, extraction.
Do NOT use for: routing, retries, deterministic transforms.
If code can answer, code answers.

## Rule 6: Token budgets are not advisory
Per-task: 16,000 tokens. Per-session: 30,000 tokens.
If approaching budget, summarize and start fresh.
Surface the breach. Do not silently overrun.

## Rule 7: Surface conflicts, don't average them
If two patterns contradict, pick one (more recent / more tested).
Explain why. Flag the other for cleanup.

## Rule 8: Read before you write
Before adding code, read exports, immediate callers, shared utilities.
If unsure why existing code is structured a certain way, ask.

## Rule 9: Tests verify intent, not just behavior
Tests must encode WHY behavior matters, not just WHAT it does.
A test that can't fail when business logic changes is wrong.

## Rule 10: Checkpoint after every significant step
Summarize what was done, what's verified, what's left.
Don't continue from a state you can't describe back.

## Rule 11: Match the codebase's conventions, even if you disagree
Conformance > taste inside the codebase.
If you think a convention is harmful, surface it. Don't fork silently.

## Rule 12: Fail loud
"Completed" is wrong if anything was skipped silently.
"Tests pass" is wrong if any were skipped.
Default to surfacing uncertainty, not hiding it.

---

## The product

**Stargate** is a small, local, vendor-agnostic orchestrator that lets several coding-agent
CLIs collaborate on one task without concurrently editing the same checkout.

An agent here is **a command prefix**, not an SDK. `claude -p ...`, `codex exec ...` and a
Kiro wrapper are configured as argument lists; stargate appends the prompt as the last
argument and reads whatever comes back. That is why one workflow can mix vendors or run all
four roles through the same CLI.

Four roles, mapped to agents under `workflow:`:

| role | runs in | may write? |
|---|---|---|
| `architect` | the user's **real** repository | no (read-only via a vendor flag) |
| `developer` | the run's isolated worktree | yes |
| `reviewer` | the run's isolated worktree | no; emits `VERDICT: APPROVED` / `CHANGES_REQUESTED` |
| `fixer` | the run's isolated worktree | yes |

Two execution modes:

- **Linear** (`stargate run`): architect → worktree → developer → tests → reviewer/fixer
  loop → terminal commit.
- **Fan-out** (`stargate run --fan-out`): the architect returns a JSON DAG instead of a
  prose plan; ready nodes run concurrently, each on its own branch and worktree, get
  committed, are merged in topological order into the integration branch, and only then go
  through the reviewer/fixer loop as one combined tree. Requires `settings.commit: true`.

**The invariants that are not up for negotiation.** The orchestrator never merges, rebases,
pushes to or deletes the branch the user has checked out; it only creates local `stargate/*`
branches and worktrees outside the repository. The **agents** remain forbidden to commit —
the orchestrator is what commits. Every run with changes that reaches a terminal result
leaves a commit on its own branch, including when the verdict is `CHANGES_REQUESTED`, when
the test command failed, or when the token budget stopped it; the verdict goes into the
commit message.

Full user documentation: [`README.md`](README.md) (1023 lines — it is the reference, do not
duplicate it here). Verified single-vendor configurations: [`examples/`](examples/).

## Repository state

**Both modes are implemented and covered.** 157 tests, all passing (`make test`, exit 0).
Current work lives on `main`; there are no open feature branches beyond the `stargate/*`
ones the runs themselves left behind.

What exists:

- `stargate/`: 10 modules, ~4,500 lines. The only runtime dependency is **PyYAML**; `ruff`
  is the only dev dependency. No agent framework, no SDK.
- The linear mode, complete: layered config, test-command detection, token budget, retries
  with backoff, timeouts, heartbeat, config/prompt snapshots, terminal commit,
  `list`/`clean`/`resume`.
- The fan-out mode, complete: DAG validation, frozen `tasks.json`, concurrent scheduler,
  dependency inheritance through commits, integration, failed-node recovery and resume.
- `resume` re-enters a recorded review/fix cycle (commit `e05e8c7`): an interrupted fixer
  does not make the run pay for a fresh review, as long as the worktree fingerprint matches.
- `doctor` and `doctor --probe`, the latter making one real (billable) call per distinct
  agent, exercising the read or write capability that role actually depends on.
- `examples/`: Claude Code, Codex CLI and Kiro CLI, each verified with `--probe`, with the
  differences between them documented (who needs `{output}`, who reports usage, who needs a
  wrapper).

What does **not** exist (do not assume; check before referencing):

- **No CI.** There is no `.github/`; the suite only runs locally.
- **Not on PyPI** (the name belongs to DataStax). Installation is `pipx install .` or from
  Git.
- **No pytest.** The suite is its own runner in `test_stargate.py`; `pyproject.toml` has no
  `[tool.pytest]`, and pytest collection would break in this repository.
- **No coverage, no type checker, no pre-commit.** `make lint` is ruff and nothing else.

A known point of friction: **commit signing**. The `commit_error` recorded in the last
fan-out run was a GPG pinentry timeout. When that happens the work stays intact and staged
in the worktree, the run exits `5`, and the message prints the manual recovery command. It
is not an orchestrator bug.

## Commands

```bash
make dev        # venv in ./.venv + editable install with [dev]
make test       # .venv/bin/python test_stargate.py  -- the whole suite
make lint       # .venv/bin/ruff check .
make install    # pipx install --force .   (make install-uv uses uv)
make uninstall
```

`make test` and `make lint` both depend on `dev`, so they run the editable install first.
The suite is heavy: every test creates a real Git repository in a
`tempfile.TemporaryDirectory()` and shells out to `python -m stargate`. Expect minutes, not
seconds.

Running a single test (the runner only accepts the whole suite from the command line):

```bash
.venv/bin/python -c "
import sys, tempfile; from pathlib import Path
sys.path.insert(0, '.')
from tests.test_fanout_graph import test_unknown_dependency_reports_the_dependent_task as t
with tempfile.TemporaryDirectory() as tmp: t(Path(tmp))
print('ok')
"
```

Exercising the orchestrator against this very repository:

```bash
stargate doctor                 # config layers, resolved commands, prompts, detected tests
stargate doctor --probe         # ONE real, billable call per distinct agent
stargate run "task description"
stargate run --fan-out --max-parallel-tasks 3 "a decomposable task"
stargate list                   # runs recorded under .stargate/runs/, newest first
stargate resume <run-id>
stargate resume <run-id> --redo developer   # linear only; fan-out rejects --redo
stargate clean <run-id>         # or --all
```

Exit codes worth knowing when debugging a run: `0` approved, `2` the reviewer still requests
changes or the arguments were invalid, `3` approved but the test command failed, `4` token
budget, `5` a verdict was reached but Git could not commit, `130`/`143`/`129` signals. The
full table is in the README.

## Architecture overview

One package, ten modules, with no abstraction layer between them. Dependencies point
downward; `core.py` imports nothing from the package.

```text
cli.py          argparse, signals, dispatch. Validates flag combinations BEFORE
                reserving any run artifact.
  └─ stages.py       the linear workflow: orchestrate() → run_stages() → review_and_finish()
       ├─ fanout.py  the fan-out workflow: DAG, concurrent scheduler, integration
       ├─ agent.py   invoking ONE agent: retries, backoff, token accounting,
       │             failure fingerprint (tells a repeated failure from a new one)
       ├─ commit.py  the terminal commit on the run's branch, and why it failed
       ├─ run.py     the run lifecycle: base ref, reserved id/branch, worktree,
       │             state.json, list/clean, worktree_fingerprint()
       ├─ config.py  layered YAML, the agent entry per role, prompts
       ├─ detect.py  guessing the test command from what is checked in
       └─ doctor.py  reporting the effective config and probing agents
            └─ core.py   StargateError, Terminated, RunContext, run_process(), git()
```

Four things explain almost all of the code:

**1. `RunContext` is the state.** A dataclass in `core.py` carrying repo, frozen config,
branch, worktree, artifacts, `done` (completed stages), `tokens_used`, `mode`, `fanout` and
`review`. It is serialized to `.stargate/runs/<run-id>/state.json` on every transition by
`save_state()`. **Every `resume` is a re-read of that file** — a new stage that does not
record its progress there is not resumable.

**2. Layered config, frozen at run start.** Order: `./.stargate.yaml` →
`~/.config/stargate/agents.yaml` → the packaged `stargate/agents.yaml`. `--config` replaces
all of it with one standalone file. The project override is deliberately named
`.stargate.yaml`, not `agents.yaml`. When the run is created, the merged config and the
prompts are copied into `.stargate/runs/<run-id>/{config.yaml,prompts/}`, and that copy is
what `resume` uses — editing the config afterwards does not change a run in flight.

**3. Everything that leaves the process goes through `run_process()`.** A single runner in
`core.py` that streams, honors the timeout, registers the process in `_ACTIVE_PROCESSES` and
kills the whole **process group**. It is what turns a SIGINT/SIGTERM/SIGHUP during a fan-out
with N concurrent agents into the already-safe resumable failure path (`Terminated`, a
subclass of `KeyboardInterrupt`) instead of leaving orphaned agents behind.

**4. Isolation is Git, not a sandbox.** Every run gets its own branch and worktree; in
fan-out so does every node. Dependency inheritance is literal: a dependent node starts from
the **commits** of its dependencies, so it sees their files. That is why fan-out requires
`commit: true` — the commit is the transport protocol between isolated worktrees.

Details that bite, when changing things:

- **The worktree is created after the architect**, on purpose, so the branch can still adopt
  the plan's vocabulary (`NAME:` on the first line, or the JSON `name` field in fan-out).
  The run id has already been printed and names directories, so it is never renamed. An
  existing worktree is stronger evidence than a missing stage bit.
- **A developer that changes nothing is an error**, and the stage is deliberately left
  incomplete so resume runs it again.
- **`test_command_detection` defaults to `report`**, not `auto`. A detected command is input
  the user never wrote and that would run after every fixer pass; the default reports the
  guess and its evidence without executing it.
- **`max_task_tokens` is only checked between phases.** A single runaway invocation
  overshoots. That is documented as a limitation, not a bug.
- **`env: {VAR: null}` on an agent entry REMOVES the variable.** It is how one agent opts out
  of a globally exported `ANTHROPIC_API_KEY` without unsetting it for the others.
- A Claude agent command **must not end in a variadic option** (`--disallowedTools` would
  swallow the prompt appended last). The packaged configs end in `--model` for this reason.

## Testing patterns

There is no pytest. `test_stargate.py` at the root is both the runner and part of the suite.

**Discovery.** `_discover_tests()` collects every module-level callable named `test_*` from
`test_stargate.py` and from each `tests/test_*.py`, sorts by name (determinism is a
requirement) and disambiguates collisions by prefixing the module. **Adding a test file
requires editing no registry.** Each test runs inside its own
`tempfile.TemporaryDirectory()`.

**Signature.** Every test is `def test_x(root: Path) -> None`. `root` is the tempdir; the
test builds whatever it needs in there and leaves nothing outside it.

**Where to write.** `tests/test_<area>.py`, one module per area — currently
`test_fanout_graph`, `test_fanout_scheduler`, `test_fanout_signals`, `test_fanout_git_state`,
`test_fanout_finish`, `test_fanout_cli_docs`, `test_review_cycle`, `test_harness`. Do not add
new tests to `test_stargate.py`; it is the history. Shared helpers go in `tests/harness.py`,
never duplicated.

**The two levels:**

- **Subprocess integration** (most of them): `make_repo(root)` creates a real Git
  repository, `write_config()` / `write_fanout_config()` write a YAML with fake agents, and
  `run()` / `doctor()` / `runs()` / `clean()` / `stargate()` shell out to `python -m stargate`
  with `PYTHONPATH=ROOT`, capturing stdout, stderr and the exit code. Git is real, the
  worktrees are real, the orchestrator is real; only the agents are faked.
- **In-process unit**: import the function directly (`from stargate.fanout import
  parse_task_graph`) and reach for `unittest.mock.patch` when something must be isolated.
  Used for DAG validation, state invariants, and whatever does not justify a subprocess.

**Fake agents are `/bin/sh -c` scripts.** `agent(script)` returns `["/bin/sh", "-c",
script]`, and the prompt the orchestrator appends lands in `$0`. Hence the suite's idioms:

```python
agent('echo change >> impl.txt; echo done')       # a developer that changes something
agent('echo "VERDICT: APPROVED"')                 # a reviewer that approves
agent('printf "%s" "$0" > /dev/null; echo done')  # a noop that consumes the prompt
agent(f'cat {graph}')                             # a fan-out architect
```

An agent that needs to **count** invocations writes to a marker file inside `root` (see
`_counting` in `tests/test_review_cycle.py`); that is how you prove a resume did not pay for
one review too many.

**Rules the suite holds itself to:**

- A test names the behavior and the why, not the function:
  `test_unapproved_result_is_committed_and_labelled`, not `test_commit_run`. If the name does
  not say what breaks when the test fails, it is wrong.
- Every subprocess assertion carries the output as its message:
  `assert "..." in proc.stdout, proc.stdout`. A failure has to be diagnosable without
  reproducing it.
- No network, no real agents, no writes outside `root`. The only path to a billable call is
  `doctor --probe`, and the suite does not exercise it.
- Fixed a defect? Add the focused regression test in the same commit. Every module under
  `tests/` was born that way.
- `make lint` (ruff `E,F,I,UP,B`, line length 100) covers the tests too.

---

<!-- code-review-graph MCP tools -->
## MCP Tools: code-review-graph

**IMPORTANT: This project has a knowledge graph. ALWAYS use the
code-review-graph MCP tools BEFORE using Grep/Glob/Read to explore
the codebase.** The graph is faster, cheaper (fewer tokens), and gives
you structural context (callers, dependents, test coverage) that file
scanning cannot.

### When to use graph tools FIRST

- **Exploring code**: `semantic_search_nodes` or `query_graph` instead of Grep
- **Understanding impact**: `get_impact_radius` instead of manually tracing imports
- **Code review**: `detect_changes` + `get_review_context` instead of reading entire files
- **Finding relationships**: `query_graph` with callers_of/callees_of/imports_of/tests_for
- **Architecture questions**: `get_architecture_overview` + `list_communities`

Fall back to Grep/Glob/Read **only** when the graph doesn't cover what you need.

### Key Tools

| Tool | Use when |
|------|----------|
| `detect_changes` | Reviewing code changes; gives risk-scored analysis |
| `get_review_context` | Need source snippets; token-efficient |
| `get_impact_radius` | Understanding blast radius of a change |
| `get_affected_flows` | Finding which execution paths are impacted |
| `query_graph` | Tracing callers, callees, imports, tests, dependencies |
| `semantic_search_nodes` | Finding functions/classes by name or keyword |
| `get_architecture_overview` | Understanding high-level codebase structure |
| `refactor_tool` | Planning renames, finding dead code |

### Workflow

1. The graph auto-updates on file changes (via hooks).
2. Use `detect_changes` for code review.
3. Use `get_affected_flows` to understand impact.
4. Use `query_graph` pattern="tests_for" to check coverage.
