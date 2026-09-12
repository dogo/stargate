# Stargate

**Your coding agents, working as a team. On one task. Without stepping on each
other.**

Stargate is a small, local orchestrator that gives four roles — architect,
developer, reviewer, fixer — to whichever agent CLIs you already pay for, and
runs them against your repository with Git as the isolation boundary. No
framework, no SDK, no daemon, no cloud: one Python package, one runtime
dependency (PyYAML), and a `pipx install`.

```bash
cd ~/dev/my-project
stargate run "Add pagination to the users endpoint"
```

That plans in your real checkout (read-only), implements in a throwaway
worktree, runs your tests, reviews the diff, fixes what the review flags, and
leaves you a commit on its own branch. Your working tree never moves.

### Why it exists

One agent in one terminal is a single point of judgment: the author reviews its
own work, and every retry costs the same context again. Adding terminals does
not fix it — N agents on one checkout is a race, and the more of them you run,
the more of your attention goes to refereeing instead of the work.

Stargate splits the roles instead. The planner cannot write. The reviewer has
never been the author. The developer works in an isolated worktree it cannot
escape. And an agent is **a command prefix**, not an integration: anything you
can run non-interactively and read the output of qualifies, configured as an
argument list. So you can put the sharpest model on review and a cheaper one on
typing, mix vendors, or run all four roles through one CLI — and adding a new
one is a few lines of YAML, not a plugin.

### The flow

```text
you
 │
 ▼
Claude / architect (read-only planning)
 │
 ▼
Git worktree + dedicated branch
 │
 ▼
Codex / developer (implementation)
 │
 ▼
tests (optional)
 │
 ▼
Kiro / reviewer
 │
 ├── APPROVED ─────────────► orchestrator commit ──► done
 │
 └── CHANGES_REQUESTED
          │
          ▼
      Codex / fixer
          │
          ▼
       tests
          │
          └──────────────► review again
```

Got work that splits? [`--fan-out`](#fan-out) turns the plan into a validated
DAG: ready nodes run concurrently, each on its own branch and worktree, a
dependent node starts from its dependencies' **commits** so it sees their files,
and the merged tree goes through review once as a whole.

### What you can count on

- **Nothing is ever lost.** Every terminal result with changes is committed on
  the run's own branch — including a reviewer that still requests changes, a
  failing test command, or a token-budget stop. The verdict goes in the commit
  message. Fan-out also commits each completed task and the integration verdict.
- **A crash is resumable, not wasted.** A crashed run creates no terminal
  commit; `resume` reuses the frozen config, prompts, branches, worktrees and
  completed commits, and commits when the run reaches a verdict. It even
  re-enters a recorded review/fix cycle, so an interrupted fixer does not pay
  for a fresh review. (A fan-out token-budget stop before integration writes its
  RESULT and stays resumable, without an integration commit yet.)
- **Your branch is untouchable.** The orchestrator never merges into, rebases,
  pushes, or deletes the branch checked out in your repository, and never
  removes a worktree mid-run. The agents themselves are forbidden to commit —
  the orchestrator is the only thing that does.
- **The budget is yours.** Token budget per task and per run, timeouts, retries
  with backoff, and `--max-review-loops`. Exit codes tell you which wall you hit.

### Agents

**Claude Code**, **Codex CLI**, and **Kiro CLI** are validated integrations, not
a closed list of supported agents. The packaged configuration uses Claude for
the architect and reviewer and Codex for the developer and fixer; the
[`examples/`](examples/) directory includes verified single-vendor
configurations for all three CLIs and documents the differences between them
(who needs `{output}`, who reports usage, who needs a wrapper). Anything with a
non-interactive command prefix works — `stargate doctor --probe` tells you
whether yours does, in one real call per agent.

## Requirements

- Python 3.10+
- Git
- The agent CLI or CLIs referenced by your effective configuration, installed
  and authenticated

The packaged default requires Claude Code and Codex CLI. A single-vendor setup
can instead start from one of the verified configurations in
[`examples/`](examples/); the Kiro configuration also uses its included wrapper.

## Install (global)

```bash
pipx install .                # or: uv tool install .
```

```bash
pipx install git+https://github.com/dogo/stargate.git
```

Not on PyPI (the name is taken by DataStax), so install from the repo or a copy
of this directory. Both prompts and the default
config ship inside the package, so once installed the source directory can be
deleted or moved. `make install` / `make install-uv` are the same commands;
`make uninstall` removes it.

Hacking on the orchestrator itself:

```bash
make dev      # editable install into ./.venv
make test     # smoke test with fake agents
```

## Configure

Seed a user-level config:

```bash
stargate init-config         # writes ~/.config/stargate/agents.yaml
```

Without `--config`, every existing file in this lookup chain is layered, most
specific first:

1. `./.stargate.yaml` in the repo you are standing in
2. `~/.config/stargate/agents.yaml` (or `$XDG_CONFIG_HOME`)
3. the `agents.yaml` packaged with the install

`settings` and `workflow` merge by key, and `agents` merges by agent name. An
agent entry is replaced as a whole, so redefining one must include its complete
`command`, `probe`, `probe_expect`, `usage_pattern` and `env` as needed; entries
for all other agents are inherited. Top-level scalar values use the
most-specific value. A blank mapping section such as `settings:` does not erase
inherited settings.

`--config <path>` is the exception: that file is used exactly as given, with no
project, user or packaged config layered under it. This is also the escape hatch
for resuming with a repaired agent definition.

`stargate doctor` numbers every source and marks the source of each effective
setting, agent entry and workflow remap. Per-project overrides use
`.stargate.yaml`, not `agents.yaml`, and lookup never walks to a parent directory
or follows paths named by a config, so a global install cannot accidentally read
configuration from an unrelated repository.

## Settings reference

Everything under `settings:` in the layered effective config. All are optional.

| key | default | meaning |
|---|---|---|
| `test_command` | `""` | Shell command run in the worktree after the developer and after every fixer pass. The same effective command is available to agent commands through `{test_command}`, which the packaged reviewer uses so it can verify the suite. An explicit value always wins over detection. |
| `test_command_detection` | `report` | What to do when `test_command` is empty: `report` shows likely commands without running one, `auto` runs the highest-priority match, and `off` skips detection. |
| `commit` | `true` | Commit results on the run's own branch. Linear runs can set this to `false`, or pass `--no-commit`, to leave the worktree dirty. Fan-out requires `true` and rejects `--no-commit`. |
| `max_review_loops` | `2` | Fixer passes allowed after the first review. `0` reviews once and stops. Overridable per run with `--max-review-loops`. |
| `blocking_severities` | `[]` | Opt-in severity policy. Empty or absent: the reviewer's explicit verdict decides. A YAML list of `high`, `medium`, `low` derives the verdict from JSON findings. A bare string or an unknown severity is rejected before any agent runs. See [Severity policy](#severity-policy-opt-in). |
| `max_fanout_tasks` | `8` | Maximum number of DAG nodes the fan-out architect may return. Must be a YAML integer: a quoted `"8"` is rejected, not coerced. |
| `max_parallel_tasks` | `2` | Maximum ready fan-out tasks run concurrently. Must be a YAML integer, like `max_fanout_tasks`. Overridable for fan-out runs and resumes with `--max-parallel-tasks`; linear invocations reject that flag. |
| `max_task_tokens` | `0` | Stop between phases once agents have reported this many tokens. `0` means no limit. |
| `agent_timeout_seconds` | `1800` | Kills a single agent invocation. `0` means no timeout. |
| `agent_retries` | `0` | Retries after a failed agent invocation. `0` preserves the single-attempt behavior; `2` allows up to three attempts. |
| `agent_retry_backoff_seconds` | `10` | Initial retry wait. It doubles after each failure: the default waits 10s, then 20s, then 40s. |
| `test_timeout_seconds` | `900` | Kills the test command; a timeout counts as exit 124. |
| `probe_timeout_seconds` | `120` | Kills a `doctor --probe` call. Deliberately far shorter than the agent timeout. |
| `worktree_root` | `""` | Where worktrees are created. Empty means `<repo-parent>/.stargate-worktrees/<repo-name>/`. |
| `prompts_dir` | `""` | Directory of custom `<role>.md` prompts, checked before the user and packaged ones. Relative paths resolve against the repo you run in. |

Per-agent keys live on the agent entry, not here: `command`, `probe`,
`probe_expect`, `usage_pattern`, `env`.

## Exit codes

| code | meaning |
|---|---|
| `0` | Approved, and the test command passed or no command ran (`report`, `off`, or no match). |
| `1` | An agent errored or timed out, configuration was invalid, or another operational error stopped the command. `resume` is offered when a run was already created. |
| `2` | The CLI arguments were invalid, the reviewer still requested changes after the last allowed fixer pass, or the fixer changed nothing and the loop stopped early. |
| `3` | Approved, but the explicit test command or an `auto`-detected command failed. |
| `4` | `max_task_tokens` was reached; the run stopped between phases. A linear stop goes through the normal RESULT/summary/commit path. A fan-out stop before integration also prints a RESULT block and writes `summary.md`, but records resumable task state without an integration terminal commit. |
| `5` | The run reached a verdict, but Git could not create its commit. The work remains intact and staged where possible; the error prints a manual recovery command. |
| `129` | The run received SIGHUP. Its state is recorded as failed and `resume` is offered. |
| `130` | The run was interrupted with Ctrl-C/SIGINT. Its state is recorded as failed and `resume` is offered. |
| `143` | The run received SIGTERM. Its state is recorded as failed and `resume` is offered. |

`doctor` exits `1` when a binary is missing, a prompt cannot be resolved, or a
`--probe` call fails.

`list` exits `0` even when a run has an unreadable or stale `state.json`. It
exits `1` when the current directory is not inside a Git repository or the runs
directory itself cannot be read.

## Check setup

```bash
stargate doctor
```

It prints the numbered config layers and provenance of the effective settings
and agents, each role's resolved command, all five resolved prompt files
(including `fanout.md`), and the configured or detected project test commands
with their evidence. It makes no external calls, so `FOUND` means only that the
executable is on `PATH` — see
[Probing agents](#probing-agents) to actually verify that an agent can run.
The effective-settings table reports values and provenance. Most settings are only
validated by the command that uses them, but an invalid `blocking_severities` is reported
here as an `ERROR` and exits `1`, with the offending value still shown in the table —
finding out from `doctor` is the point of `doctor`.

## Probing agents

`doctor` alone never calls an agent. `doctor --probe` makes one real,
**billable** call per distinct agent command. It tests the file capability that
role depends on, so a working credential paired with a broken tool layer fails
in seconds instead of being discovered after a paid planning stage:

```console
$ stargate doctor --probe
Agent probes:
  OK   architect, reviewer (read) [4.3s]
  FAIL developer, fixer (write) [6.1s]
       agent exited 0 but did not write probe-1.txt; its file-editing tools are not working
```

The prompt and required capability live in the config, so the orchestrator
stays vendor-agnostic. `{probe_file}` becomes an absolute path inside the
throwaway repository, and `probe_expect` may be `read` or `write`:

```yaml
architect:
  command: [claude, -p, --output-format, text, --model, opus]
  probe: "Read the file {probe_file} and reply with its contents, nothing else."
  probe_expect: read
```

For a read probe, stargate seeds the file with a random marker and requires the
agent to return it. For a write probe, the file starts absent and the agent must
create it non-empty. The packaged architect and reviewer use `read`: their
`--disallowedTools "Edit Write NotebookEdit"` setting intentionally prevents
writes, so demanding one would be a false alarm. The developer and fixer use
`write` because editing is their job.

Because the probe runs the agent's *real* command, it also catches a wrong
model name or unsupported flag. The existing `{output}` contract remains in
force: an agent that declares it and exits 0 without writing the final-message
file fails the probe. Agents with no `probe` key report `SKIP` and do not fail
the exit code. Probes remain opt-in, run in a throwaway Git repository rather
than yours, and use `probe_timeout_seconds` (120) rather than the much longer
agent timeout. Agent-command placeholders are expanded for probes too, so
`doctor --probe` verifies the same effective command that a real role uses.

## Use it against a repository

Stand in the repository you want the agents to modify:

```bash
cd ~/dev/my-project
stargate run "Add pagination to the users endpoint"
```

Before creating run artifacts or calling an agent, Stargate resolves the base
ref to a commit and checks a local branch against its configured upstream. It
stops if the branch is behind or diverged, then uses the frozen commit for the
whole run so a concurrently moving branch cannot change the developer's base.
For a remote upstream it queries the remote directly without fetching; if the
remote cannot be checked, the run does not start.

Per-project settings (usually just the test command) go in a `.stargate.yaml`
at the repo root:

```yaml
settings:
  test_command: "npm test"
```

Choose another starting ref:

```bash
stargate run \
  --base-ref main \
  "Add passkey authentication"
```

Limit review/fix loops:

```bash
stargate run \
  --max-review-loops 1 \
  "Refactor the image cache"
```

Give the run and branch a short name before anything is created:

```bash
stargate run --name "passkey auth" "Add passkey authentication to account settings"
```

`--name` takes precedence over the architect's suggestion and uses at most five
whole words (32 characters), never a word truncated in the middle.

## Fan-out

Use `--fan-out` when one request contains work that can be split across
independent branches:

```bash
stargate run --fan-out --max-parallel-tasks 3 \
  "Add passkey authentication across the backend, frontend, and docs"
```

The architect returns a validated `tasks.json` instead of a prose plan. Each
node has an ID and a self-contained task; `acceptance` and `depends_on` are
optional lists, and an explicit empty list is equivalent to omitting either
field. Stargate rejects duplicate or malformed IDs, unknown, duplicate or
self-dependencies, cycles, malformed JSON, and graphs larger than
`max_fanout_tasks` before a developer starts.

Ready nodes run concurrently, each on its own branch and worktree. A dependent
node starts from the commits produced by its dependencies, so it sees their
files rather than merely waiting for them. Completed nodes are committed by the
orchestrator, merged in topological order into the run's integration branch,
tested together, and passed through the normal reviewer/fixer loop as one
combined tree.

Fan-out requires `commit: true`: commits are the protocol that moves work
between isolated worktrees, even though nothing is merged into the user's
original branch or pushed. `--no-commit` is rejected before a new run is
created. `resume` restores fan-out mode from `state.json`; it reuses the frozen
config and prompts, `tasks.json`, task branches/worktrees and every completed
task commit, plus the integration branch/worktree if they were already created.
Only unfinished or failed nodes are retried. The effective parallelism can be
changed when resuming with `--max-parallel-tasks`; `--redo` is not supported for
fan-out resumes.

The contract produced by the architect is:

```json
{
  "name": "passkey authentication",
  "tasks": [
    {
      "id": "api-contract",
      "task": "Define the passkey data model and API contract.",
      "depends_on": [],
      "acceptance": ["Request and response shapes are documented"]
    },
    {
      "id": "backend",
      "task": "Implement the passkey endpoints and focused tests.",
      "depends_on": ["api-contract"],
      "acceptance": ["Registration and authentication tests pass"]
    }
  ]
}
```

Tasks that modify the same files should be joined by a dependency or kept as
one node. Git conflicts during dependency preparation or final integration stop
the run without merging into the original checkout; fix the task split or the
preserved branch, then resume.

## What gets created

Suppose the architect names the work `passkey auth`. The implementation gets a
short branch similar to:

```text
stargate/passkey-auth-20260830-181500
```

When the run reaches a terminal result, this branch also receives a local
commit. The user's `main` (or other checked-out branch), index and worktree do
not move. A follow-up can branch directly from it:

```bash
stargate run --base-ref stargate/passkey-auth-20260830-181500 "Add recovery codes"
```

For a linear run, the packaged architect prompt asks for a `NAME:` first line.
Stargate strips a valid line before forwarding the plan to the developer,
reviewer, and fixer. A custom/older prompt that omits it, or a malformed name,
safely falls back to the task slug and leaves the plan untouched. The fan-out
prompt carries the corresponding name in the top-level JSON `name` field.

The run ID and artifacts directory must exist before the architect runs, so an
architect suggestion renames the branch only. Use `--name "passkey auth"` when
the run ID should also be short; that produces
`20260830-181500-passkey-auth`. Existing run IDs are not parsed or migrated, so
they continue to work with `list` and `resume`. If a name is reserved twice in
the same second, both the run ID and branch receive the same `-2`, `-3`, …
discriminator.

And its own worktree outside the target repository:

```text
<repo-parent>/
├── my-project/
└── .stargate-worktrees/
    └── my-project/
        └── 20260830-181500-add-passkey-authentication/
```

Run artifacts are stored under the target repo:

```text
my-project/.stargate/runs/<run-id>/
├── state.json         # stage, status, commit, review cycle -- what `resume` reads
├── config.yaml        # the fully merged effective config, frozen at run start
├── prompts/           # role and fan-out prompts, frozen at run start
├── plan.md
├── plan.md.log        # the agent's full trace, written live
├── plan.md.attempt-2.log  # later attempts keep separate traces
├── developer.txt
├── developer.txt.log
├── review-1.md
├── fix-1.txt          # only when needed
├── tests-*.txt        # if an explicit or auto-detected command runs
├── commit-message.txt # exact traceable message passed to Git, if attempted
└── summary.md
```

A fan-out run additionally stores `tasks.json` and one `tasks/<id>/` artifact
directory per node. Each task branch is normally `<run-branch>-<task-id>` (with
a numeric suffix if that branch already exists).
The integration worktree uses the normal run path. Each task worktree is its
sibling at `<run-worktree-parent>/<run-id>-<task-id>`.

The tree above shows the linear stage filenames. Fan-out uses
`architect-tasks.json` and `tasks.json` in place of `plan.md`, then records each
task separately:

```text
my-project/.stargate/runs/<run-id>/
├── architect-tasks.json      # architect's raw final response
├── architect-tasks.json.log  # architect's full trace
├── tasks.json                # validated, normalized DAG reused by resume
└── tasks/
    └── <id>/
        ├── developer.txt
        ├── developer.txt.log
        ├── state.json
        ├── tests-task.txt     # when a test command runs
        ├── commit-message.txt # when the task commit is attempted
        └── result.json        # after successful task completion
```

Integration tests, reviews, fixes, the terminal commit message and `summary.md`
remain at the run-artifact root. A fan-out budget stop before integration writes
`summary.md`; resume continues from the saved task state.

Each role prints its `.log` path before starting, so it can be followed with
`tail -f`, and its exit code and duration when it ends. While an agent runs it
prints a heartbeat every 30 seconds — elapsed time and bytes written — so a
long silent stage is visibly moving rather than possibly hung:

```console
=== DEVELOPER ===
trace: tail -f .../developer.txt.log
  ... 30s elapsed, 41,238 bytes written
  ... 60s elapsed, 96,004 bytes written
[developer] exit 0 in 512s
```

The trace itself is never echoed to the terminal.

`.stargate/` ignores itself (it writes its own `.gitignore`), so run
artifacts never show up in the target repo's `git status`.

## Configure tests

When `test_command` is empty, stargate inspects the repository and prints every
likely test command and the file that supplied the evidence. `stargate doctor`
shows the same information before a run. Detection has a fixed priority rather
than accidental first-match-wins:

1. `make test` for a `test` target in `Makefile`, `makefile`, or `GNUmakefile`
2. `npm test`, `pnpm test`, or `yarn test` for a non-placeholder
   `package.json` `scripts.test` (lock files choose pnpm/yarn)
3. `cargo test` for `Cargo.toml`
4. `go test ./...` for `go.mod`
5. `swift test` for `Package.swift`
6. `pytest -q` only with explicit pytest configuration/dependencies or
   `tests/test_*.py`

A root-level `test_*.py` alone is deliberately not a pytest signal: projects
often have executable smoke tests with custom arguments that pytest cannot
collect. Detection reads the original repository once, before agent edits, so
a developer cannot cause a newly written command to execute later in the same
run.

The default mode is `report`. A detected command is a command the user did not
write into stargate configuration, and the wrong guess could invoke a watcher,
an unexpectedly broad suite, or a misleading reviewer verdict several times.
Report-only mode fixes the previous silent failure without taking that extra
authority: it prints the top choice and alternatives, and tells the reviewer
what was found but not run.

Confirm the project command explicitly in `.stargate.yaml`:

```yaml
settings:
  test_command: "pytest -q"
```

Or opt into automatically running the highest-priority match:

```yaml
settings:
  test_command_detection: auto
```

Set the mode to `off` when the repository intentionally has no orchestrator
test command. An explicit `test_command` always wins and suppresses detection.

Examples:

```yaml
test_command: "swift test"
```

```yaml
test_command: "npm test"
```

```yaml
test_command: "xcodebuild test -scheme MyApp -destination 'platform=iOS Simulator,name=iPhone 17 Pro'"
```

The command is intentionally project-specific. It runs through `/bin/sh -lc`
inside the agent worktree, after the developer and after every fixer pass. The
tail of its output is fed into the reviewer and fixer prompts, so a red suite
is a blocking review finding rather than a number nobody reads. The packaged
reviewer may also run that exact command in the worktree, letting it reproduce
the result itself instead of trusting the pasted report alone.

The reviewer grant follows only a command Stargate itself selected. An explicit
`test_command` is granted, as is the top detection candidate in `auto` mode.
`report`, `off`, an empty setting, or no detected command grants nothing. A
report-only candidate remains unapproved even though `doctor` and the reviewer
prompt show it.

Timeouts (seconds) cap a stuck agent or suite:

```yaml
settings:
  agent_timeout_seconds: 1800
  test_timeout_seconds: 900
```

## Customize prompts

The version 5 packaged configuration includes five prompts to tune: four role
prompts plus the fan-out architect contract. Copy them somewhere writable:

```bash
stargate init-prompts        # writes ~/.config/stargate/prompts/*.md
```

Then delete the ones you don't want to change. Lookup is per-file, first hit
wins:

1. `settings.prompts_dir` from the active config, if set
2. `~/.config/stargate/prompts/`
3. the prompts packaged with the install

So keeping only a custom `reviewer.md` leaves the other four on the defaults.
`stargate doctor` prints the file each prompt resolved to.

The packaged `architect.md` also asks the architect to begin its response with
`NAME: <two to four words>`. Stargate treats that line as optional for custom
and frozen prompts: only a valid first non-empty `NAME:` line is removed, and
the rest of the plan is forwarded unchanged.

`fanout.md` is separate because its output is machine-readable. It requires a
bare JSON object and receives `{max_tasks}` in addition to the task and base
ref; malformed output stops before any implementation work starts.

Prompts committed with a project:

```yaml
# .stargate.yaml
settings:
  prompts_dir: .stargate-prompts   # relative to the repo you run in
```

Two things the prompt templates have to respect:

- Only known placeholders are substituted, by literal replacement: `{task}` and
  `{base_ref}` everywhere, plus `{plan}` (developer, reviewer, fixer),
  `{tests}` (reviewer, fixer), `{review}` (fixer) and `{known_findings}` (architect only).
  An architect template that omits `{known_findings}` simply receives no inherited section,
  including custom templates and older runs' frozen prompts. Every other brace is left
  alone, so a prompt may contain JSON, CSS or an f-string example verbatim. The
  fan-out prompt additionally receives `{max_tasks}`.
  These are separate from the agent-command placeholders described below.
- `reviewer.md` is a contract with the orchestrator, and either form satisfies
  it: one JSON object as described in
  [Structured review findings](#structured-review-findings), or a response whose
  last line is exactly `VERDICT: APPROVED` or `VERDICT: CHANGES_REQUESTED`.
  Anything else aborts the run.

## Structured review findings

The packaged `reviewer.md` asks for one JSON object and nothing else:

```json
{
  "verdict": "CHANGES_REQUESTED",
  "findings": [
    {
      "severity": "high",
      "file": "app/storage.py",
      "line": 42,
      "finding": "Saving truncates the existing file before validating the replacement.",
      "why": "Invalid input permanently destroys previously saved user data."
    }
  ]
}
```

`verdict` is required and must be exactly `APPROVED` or `CHANGES_REQUESTED`. **By default,
the orchestrator acts on the reviewer's decision as given.** Only an explicit
`settings.blocking_severities` policy can override it. `findings` may be empty. Each entry needs a
`severity` of `high`, `medium` or `low` and a non-empty `finding`; `file`, `line` and `why`
are optional, and unknown keys are ignored. A `CHANGES_REQUESTED` with no findings, or an
`APPROVED` carrying a `high` one, is accepted rather than treated as an error.

Without a policy, severity is reported but does not decide approval or select what the
fixer works on. It is classified by demonstrated impact — the packaged prompt carries the
full rubric. It orders the summary table, and a value outside the three labels aborts the
run as a malformed review.

The findings reach three places:

- `{review}` in the fixer prompt receives the reviewer's response with no findings
  renderer applied, JSON or prose. Its content is never rewritten — a finding containing
  the literal text `{tests}` reaches the fixer as the reviewer wrote it — though the outer
  whitespace is trimmed.
- `summary.md` gets a `## findings` table when the findings are non-empty, highest
  severity first. The stored order remains the reviewer's.
- `state.json` records them under `findings`, separate from the `review` checkpoint that
  `resume` uses. An empty list is stored as `null`, like the neighbouring `fanout` and
  `review` keys, so a consumer must not assume the value is iterable. The checkpoint ends
  with the run — except when the commit failed, which keeps it so `resume` can retry only
  the commit — while the findings stay. That is what lets the summary of an approved resume
  that skips the review still show its table.

Both hold the **most recent completed review** only, which is not always the tree the run
ends on: a fixer edit followed by a token-budget stop finishes with the previous review's
findings, and the summary does not label which pass they came from. A later review replaces
them. The per-pass `review-N.md` artifacts hold the
passes still available: resuming a finished run starts the numbering over and can overwrite
`review-1.md`, so they are not an immutable history.

Who decides approval did not change with the V1 contract, though what the parser accepts
did: bare JSON now succeeds where it used to abort, and a malformed payload is a new way to
stop. A reviewer that answers in prose, ending
in `VERDICT: APPROVED` or `VERDICT: CHANGES_REQUESTED`, is still read exactly as before —
which is what keeps a custom `prompts_dir` reviewer working, and what lets a run created
before this existed still `resume` against its frozen prompt. What a new prompt can change is
the model's own judgment, and malformed output is a new way for a run to stop: neither is a
change to the orchestrator's rules.

### Severity policy (opt-in)

The default is unchanged: absent `settings.blocking_severities`, `[]`, or YAML `null`
keeps the reviewer's explicit verdict. To opt in, configure a list:

```yaml
settings:
  blocking_severities: [high, medium]
```

For a JSON review with findings, the orchestrator derives `CHANGES_REQUESTED` when **any
finding has a severity in that list**, and `APPROVED` otherwise. The derived verdict
controls the review/fix loop, summary, exit code and terminal commit message. Findings
remain in the report even when they do not block. This applies to both linear runs and
the combined integration review in fan-out; task scheduling is unchanged.

- A review with **no findings** always keeps its explicit verdict, including
  `CHANGES_REQUESTED`: there is no evidence to justify deriving an approval.
- **Prose** always keeps its literal final `VERDICT:` line, regardless of policy.
  It carries no structured findings to derive from.
- The fixer still receives the reviewer's response unchanged, with no findings renderer.
  When the policy is active, a separate orchestrator note accompanies the review in the
  fixer prompt, naming the blocking severities and explaining these exceptions.

The setting must be a YAML list: a bare `high`, an empty string, or an unknown name such
as `critical` fails before any agent is called. Names are case-insensitive, surrounding
whitespace is ignored, and duplicates are collapsed. `stargate doctor` shows the value
and which config layer supplied it.

A resume normally uses the run's frozen config. An explicit `--config` can tighten,
loosen, or disable the policy. While the worktree fingerprint still matches, Stargate
re-evaluates the saved `review-<n>.md` under that policy: saved findings can go straight
to the fixer or become approved without paying for another review of the same tree.
Re-evaluating an active or newly disabled policy needs a usable artifact; a missing or
malformed one requires a fresh review. Be aware of what that re-reading implies: while a
policy is active the artifact **outranks** the recorded verdict in both directions, so a
hand-edited `review-<n>.md` claiming `APPROVED` with no findings can approve a run that
recorded `CHANGES_REQUESTED`, with only the worktree fingerprint as a guard. Without a
policy, V1's rule still holds — an artifact can confirm recorded changes but never create
an approval. An active policy is recorded as
`review.blocking_severities` in the checkpoint so later resumes can recognize its removal,
including when it was enabled by an earlier resume. Runs that never used a policy keep
V1's replay rules: an approval retries the commit without reading the artifact, and
recorded changes reuse only an artifact that still requests changes.
The existing review-loop budget still applies: if a saved pass falls outside a
reduced budget, it is not replayed.

**There is no behavior-neutral derived default.** A lenient list such as `[high, medium]`
can approve a low-only `CHANGES_REQUESTED`; a strict list such as `[high, medium, low]`
can block an `APPROVED` carrying findings. Activating either direction changes approval
behavior. That is why the policy is off by default. The model still judges severity:
**a misclassified severity can end a run before the fix.**

### Inheriting findings across runs

For linear runs, `stargate run --base-ref stargate/<previous-branch> "next task"`
automatically includes the previous run's findings in the architect prompt. This is the
continuation command printed at the end of a committed run; no opt-in flag is needed.
The resolved base ref must match a recorded `state.json` **branch** field exactly: Stargate
scans `.stargate/runs/*/state.json`, without trying to reconstruct a run id from the branch
name. The current branch also qualifies when `--base-ref` is omitted. A SHA, tag or
`refs/heads/stargate/...` spelling does not qualify.

Only the immediately previous run's **last completed review** travels, including findings
from an `APPROVED` review. All findings pass through, unfiltered and without a size cap,
so a large review can lengthen the prompt; earlier runs are not accumulated. The
`## Known unresolved findings` section names the source run and warns that the findings
describe the tree that review saw: an edit after that review may already have resolved one.
The architect checks the current code and plans a fix or explains why a finding stays.
Inheritance injects text only into the architect prompt and does not change verdict rules.

A non-Stargate base ref, no matching run, unreadable state, or absent, `null` or empty
findings silently adds nothing: no heading, empty section, warning or error. Fan-out runs
do not inherit findings.

**Inheritance therefore requires `commit: true`,** the same way fan-out does. A run whose
commit was disabled or failed leaves a branch that still points at the base, so it does not
carry the work its findings describe; inheriting from it would hand the architect a review
of a tree the new run cannot see. Such a run is skipped like any other case inheritance
cannot answer. With `commit: false`, `--base-ref stargate/<branch>` gives you neither the
previous run's code nor its findings.

## Final message vs. stdout

What an agent *prints* is not what it *answered*. `codex exec` streams the whole
session to stdout — reasoning, every command it ran, a token footer — while
`claude -p --output-format text` prints only the final message.

That matters because the architect's answer becomes `{plan}` in three later
prompts and the reviewer's becomes `{review}` in the fixer prompt. Forward a
trace and every hop pays for the previous hop's trace.

So an agent can be handed a file to write its last message to. Put `{output}`
anywhere in its command and the orchestrator substitutes a path, forwards
whatever lands in that file, and keeps stdout beside it as a `.log`:

```yaml
developer:
  command: [codex, exec, --sandbox, workspace-write, --output-last-message, "{output}"]
```

Agents without `{output}` keep using stdout, which is correct for the Claude
roles as configured. If a command declares `{output}` and writes nothing there,
the run stops rather than silently forwarding an empty plan.

This also protects the verdict: a reviewer's trailing session footer would
otherwise sit after the `VERDICT:` line the orchestrator parses.

Agent commands have two placeholders:

- `{output}` becomes the final-message path described above.
- `{test_command}` becomes the exact explicit or `auto`-detected command that
  Stargate will run. The packaged reviewer places it in Claude Code's
  `Bash(...)` allow rule.

When no test command will run, Stargate removes the argument containing
`{test_command}` and, for a separate option/value pair, its introducing option.
This avoids `Bash()`, an empty argv item, or a dangling option consuming the
prompt. Put the placeholder in an option value, as the packaged config does;
it cannot be the executable. Commands without the placeholder are unchanged,
including custom configs and configs frozen by older runs.

The test command is arbitrary shell text, but an allow rule is a pattern. To
keep interpolation from silently widening the reviewer's authority, Stargate
does not interpolate commands containing `(`, `)`, `,`, `*`, or control
characters. Stargate can still run such a configured command itself; the
reviewer receives only the test report. `stargate doctor` prints every expanded
agent command and says whether the grant is effective, absent, or refused.

## Stages that produce nothing

The architect already has to return a non-empty plan. The developer has a
different output contract: it must change the worktree. Tracked edits,
deletions, commits and untracked new files all count. If it exits 0 without any
change, the run fails before tests or review and the developer is not recorded
as complete, so a normal `resume` reruns it.

A fixer can legitimately conclude that a review finding needs no code change,
so the same situation is not treated as an agent failure. Re-reviewing an
identical worktree would only buy the same verdict again; stargate stops the
loop immediately with `CHANGES_REQUESTED` instead.

## Committing the result

Committing is on by default. This changes older behavior deliberately: a
default-off feature would leave every user who did not discover the setting
with the same manual branching and committing step, while completed work would
still exist only in a removable worktree. The scope of the mutation stays
narrow: Git advances only Stargate-created branches inside Stargate worktrees.
For a linear run, use `settings.commit: false` or `--no-commit` when that
tradeoff is not wanted. Fan-out requires commits and rejects both configurations.

The linear workflow commits only after a terminal verdict, never after
individual fixer passes, and therefore normally produces one commit. A resume
of already committed work finds an empty index and produces no second commit; a
later `--redo` that genuinely adds work can produce a new terminal commit.
Empty commits are not created in the linear workflow. Fan-out commits completed
tasks, merges those commits into the integration branch, then records the
integration verdict in a terminal commit; that final commit can be empty when
the merges already contain every file change.

Red outcomes that reach the finish path are durable too. `CHANGES_REQUESTED`, a
failed test command and a linear `BUDGET_EXCEEDED` are recorded in the subject
and `Stargate-Verdict` trailer rather than being presented as success. A fan-out
budget stop before integration writes a RESULT and summary but deliberately
does not put a terminal commit at the base of the integration branch. Crashes,
agent errors, invalid reviewer output and catchable signals also have no
terminal commit; a successful `resume` records the eventual integration verdict.

The commit stages tracked edits and deletions plus new untracked implementation
files. Gitignored files remain ignored. Stargate also snapshots untracked
entries around every orchestrator test command and excludes entries that first
appeared during testing, so an incomplete `.gitignore` does not silently add
`build/`, `.venv/` or similar output. Tracked files rewritten by a test,
such as an intentional snapshot or lockfile, remain part of the reviewed diff
and are committed. The untracked snapshot uses Git's directory grouping: if a
suite creates an artifact inside an untracked directory that already existed
before the suite, Git cannot distinguish the nested artifact from the
implementation directory; the durable fix for that mixed case is a project
`.gitignore`.

The subject begins with `stargate:`, includes the terminal verdict, and stays
within 72 characters. The body names the task, test result and base ref, while
Git trailers carry the run ID, verdict, base ref and test exit when available.
That run ID maps the commit back to `.stargate/runs/<run-id>/`. Git still uses
the repository's configured user, signing policy and hooks: Stargate supplies
neither `--author`, `--no-gpg-sign` nor `--no-verify`.

A failed hook, signing problem or missing Git identity returns exit code 5.
The result remains staged where possible, `state.json` keeps the terminal
verdict plus `commit_error`, and the terminal names both recoveries: `stargate
resume <run-id>`, which reuses the recorded verdict and retries only the
commit, and a manual `git commit` in the intact worktree. A hook that succeeds but rewrites files
after staging leaves those rewrites uncommitted with a warning; Stargate never
makes an automatic second commit to conceal that state.

## Retrying a transient failure

Retries are off by default. Enable them per project with a count of retries
*after* the first attempt and a base wait:

```yaml
settings:
  agent_retries: 2
  agent_retry_backoff_seconds: 10
```

This allows at most three attempts of the agent that failed, waiting 10 seconds
and then 20 seconds. A successful attempt is never repeated, nor are agents
from an already completed stage. Each attempt announces its number and wait in
the terminal and writes a separate trace such as `plan.md.log`,
`plan.md.attempt-2.log`, and `plan.md.attempt-3.log`.

Non-zero agent exits and timeouts are retried because rate limits, server
errors and dropped connections all appear that way at this boundary. A binary
that cannot start is not retried, nor is an agent that exits successfully but
violates its `{output}` contract. Stargate does not maintain a brittle list of
vendor error messages. Instead, if two consecutive failures have the same
normalized trace tail, it treats the failure as deterministic and stops early;
this avoids repeatedly paying for a wrong model name or unsupported flag. The
tradeoff is that an identical transient response twice also stops the retries.

Every retry is a fresh, potentially billable call. Reported usage is counted
once for every attempt actually made, including failed attempts, so retries can
reach `max_task_tokens` sooner. The agent timeout applies independently to each
attempt; configure the retry count and backoff with the resulting worst-case
runtime in mind. After the last attempt, the normal error and `Resume with:`
hint are preserved.

## Listing runs

`stargate list` shows the runs recorded in the current repository, newest
first, without requiring an active config:

```console
$ stargate list
Runs in /home/me/project (newest first):

* 20260831-101304-add-passkey-authentication  [failed]
    updated   2026-08-31 10:19:42  |  stage developer
    task      Add passkey authentication
    branch    stargate/add-passkey-authentication-20260831-101304
    worktree  /home/me/.stargate-worktrees/project/20260831-101304-add-passkey-authentication  (MISSING)

  20260830-181500-update-docs  [approved]
    updated   2026-08-30 18:22:11  |  stage review
    task      Update docs
    branch    stargate/update-docs-20260830-181500
    worktree  /home/me/.stargate-worktrees/project/20260830-181500-update-docs

* resumable. Resume the newest with: stargate resume 20260831-101304-add-passkey-authentication
```

The former `stargate runs` spelling remains an alias.
Each run appears in a separate block. Task summaries wrap to the terminal width
(80 columns when unavailable), with `...` after three lines if truncated.
Branch names and worktree paths remain complete for copying.

Failed runs and rows still reading `running` are marked with `*`. A `running`
row normally means the run is still in flight, but it can also mean the process
was stopped by an uncatchable hard kill such as SIGKILL or power loss. Missing
or corrupt `state.json` files are shown as unknown rather than crashing or
hiding the run. If `.stargate/runs/` does not exist or is empty, the command
reports that there are no runs. Listing only reads recorded state: it never
creates, repairs or deletes run artifacts.

## Cleaning runs

`stargate clean <run-id>` removes one run. `stargate clean --all` preflights
every recorded run and removes each run that passes; runs that fail a safety
check are skipped, each reported with its reason, and the command exits `1`.
Atomicity is per run: a single run is never partially removed, so a fan-out
run keeps all of its worktrees, branches and artifacts if any one of its
targets fails. A run is removed only when its branch
is merged into the current `HEAD` and Git accepts the worktree as clean. There
is no force option. After those checks, Stargate removes the linked worktree,
its `stargate/*` branch and the run's `.stargate/runs/<run-id>/` artifacts. If a
worktree is missing but still registered, Stargate removes only that stale Git
registration before removing the remaining branch and artifacts.

## Resuming a failed run

Every stage is recorded in the run's `state.json` before and after it runs. If
a stage fails — the CLI is out of credits, the machine sleeps, a package
upgrade removes the install mid-run — the run stops and prints:

```text
Resume with: stargate resume 20260831-101304-add-passkey-authentication
```

For a linear run, `resume` reuses the plan, branch, worktree, frozen config and
frozen prompts, and skips the stages already marked complete. It does not
produce a second plan, branch or worktree. A developer that exited successfully
but changed nothing is deliberately not marked complete, so plain `resume`
reruns it. Fan-out reuse is described in [Fan-out](#fan-out).
Untracked test-artifact exclusions are stored in `state.json`, so output
created before a crash remains excluded when a later process resumes. If a
previous invocation already committed and no new work was added, the resumed
finish detects the empty index and does not make a duplicate commit.

By default it runs under the config frozen into the run, so resuming cannot
silently change the agents the earlier stages ran under. Pass `--config` to
override that, which is how you resume past a broken agent definition:

```bash
stargate --config ./fixed.yaml resume <run-id>
```

To rerun a linear stage that was already recorded as complete, use the supported
`--redo` option instead of editing `state.json`:

```bash
stargate resume <run-id> --redo developer
```

For linear runs, `--redo` accepts `architect` or `developer` and is repeatable.
Fan-out resumes reject it. Redoing only the
architect leaves the developer marked complete, which means the new plan would
not be implemented; stargate warns in that case, and you can pass both
`--redo architect --redo developer`.

### Resuming into the review/fix loop

The review/fix cycle is recorded in `state.json` alongside a fingerprint of the
worktree the reviewer judged, so a resume re-enters the loop where it stopped
instead of buying a second opinion on the same tree:

- A **fixer that died** — a vendor usage limit, a crash — resumes straight into
  that fixer, reusing the `review-<n>.md` already on disk. The pass is
  announced as `REVIEW <n> (skipped, reusing …)`.
- A **run that reached its verdict but could not commit** — a signing prompt
  that timed out, a hook that rejected the tree — resumes straight into the
  commit, announced as `REVIEW <n> (skipped, already APPROVED)`. Such a run is
  marked resumable by `stargate list` even though it recorded a terminal
  verdict, and the commit error names `stargate resume <run-id>` as the cheap
  recovery.
- A **policy changed by `--config`**, including disabling it, re-evaluates the saved
  review against the effective policy. With an unchanged fingerprint, a recorded approval
  can now go to the fixer and recorded changes can now approve, without another review.
- **Anything that changed the worktree** since the verdict — a hand edit, a
  fixer that got partway through — invalidates the record, and the loop starts
  over with a real review. A verdict never covers a tree its reviewer did not
  see.

`max_review_loops` is a budget for the run, not a fresh allowance per resume:
a resumed loop continues the count rather than restarting it. Resuming a run
that actually finished discards the record and reviews from scratch, which is
what makes `--config` with a corrected reviewer take effect.

## Terminating a run

Ctrl-C already follows the normal failure path. SIGTERM and SIGHUP do too: the
direct agent process stargate started is killed, `state.json` is changed from
`running` to `failed` with the signal recorded, and the same `Resume with:` hint
is printed. The orchestrator exits with the conventional `128 + signal` code:
130 for SIGINT, 143 for SIGTERM and 129 for SIGHUP. Cleanup is bounded, and a
second termination signal uses the operating system's default action so it
cannot leave the orchestrator stuck.

SIGKILL and sudden power loss cannot be handled by any process. A run stopped
that way can still show as `running`; inspect it before resuming. Only the direct
agent process is killed during catchable-signal cleanup. Subprocesses it spawned,
such as test runs or tool calls, may be orphaned and continue running, so check
the worktree before resuming. Signal handling also starts only after the CLI has
enough information to enter the run path, so a termination during very early
setup may leave no state file rather than a false `running` state.

## Token budget

```yaml
settings:
  max_task_tokens: 120000     # 0 or unset = no limit
```

Be clear about what this can and cannot do. Most of a run's tokens are the
model reading the repository — that never crosses this process, so the
orchestrator cannot meter it and cannot interrupt an agent mid-flight. The
number it tracks is whatever the agent's CLI *prints*, extracted by a regex the
config supplies:

```yaml
developer:
  command: [codex, exec, --sandbox, workspace-write, --output-last-message, "{output}"]
  usage_pattern: 'tokens used\s+([\d,]+)'
```

The totals are summed across phases and checked at each phase boundary. So a
budget stops the *next* agent from starting, never one already running. Parallel
fan-out work can overshoot by the agents that were already scheduled; the RESULT
and `summary.md` report the run's true totals as `Tokens: X of Y`, so the
overshoot is visible there as a total above the cap, and the scheduler explains
it on stderr when it stops. Hitting the cap ends the
run with verdict `BUDGET_EXCEEDED` and exit code 4, leaving its worktree state
intact. A linear stop commits changes through its normal finish path. Completed
fan-out tasks already have task commits, but a stop before integration does not
create a terminal integration commit; `resume` continues from the saved task
state.

For a genuine in-flight cap, use a vendor flag in the command itself. Claude
has one; Codex has no equivalent today:

```yaml
architect:
  command: [claude, -p, --output-format, text, --max-budget-usd, "0.50"]
```

An agent with no `usage_pattern` contributes zero to the total, which makes the
budget blind to it. `stargate doctor` flags that per role whenever a cap is set.

## Per-agent environment

An agent inherits the orchestrator's environment. `env` overrides it for that
agent only, and a `null` value **removes** a variable:

```yaml
architect:
  command: [claude, -p, --output-format, text]
  env:
    ANTHROPIC_API_KEY: null       # use the CLI's own login instead
    ANTHROPIC_BASE_URL: "https://internal.example/v1"
```

The null case is the one that earns the feature. A globally exported
`ANTHROPIC_API_KEY` takes precedence over the Claude CLI's claude.ai login, so
every Claude role fails with `Credit balance is too low` while the Codex roles
are fine — and the only other fix is unsetting it for the whole orchestrator.

Two consequences worth knowing:

- Probe deduplication keys on command **and** environment. Two roles running
  the same command under different credentials are two separate probes, not
  one; deduping on the command alone would report an agent that was never
  called.
- `doctor` prints the variable **names** an agent overrides, never the values.

## Change who does what

The roles are aliases:

```yaml
workflow:
  architect: architect
  developer: developer
  reviewer: reviewer
  fixer: fixer
```

Every agent is just a command prefix. For example, assigning the reviewer role
to a Kiro agent only requires its agent definition plus:

```yaml
workflow:
  reviewer: kiro_reviewer
```

The mapping is vendor-agnostic: Claude, Codex, Kiro, or another compatible CLI
can fill any role whose permission and output requirements it supports.

## Adding a different agent CLI

Nothing in the package names a vendor: the only mentions of Claude or Codex in
`stargate/*.py` are comments and the `--help` line. An agent is six YAML keys —
`command`, `env`, `probe`, `probe_expect`, `usage_pattern`, and the `{output}` /
`{test_command}` placeholders — so adding a CLI is a config change.

Finding out *what to put* in that config is the actual work, and `doctor` only
catches the first of the four things that go wrong:

1. **It is not on PATH, or the prompt is not the last argument.** `doctor`
   reports this immediately; `doctor --probe` also proves the CLI can read and
   write when its role needs to.
2. **Its stdout is a session trace, not the answer.** Then it needs `{output}`
   (see [Final message vs. stdout](#final-message-vs-stdout)). A CLI with no
   flag for that needs a small wrapper.
3. **`usage_pattern` matches nothing, or matches the wrong number.** This fails
   silently: the run finishes and reports `Tokens reported: 0`, so a
   `max_task_tokens` budget quietly never applies.
4. **Its permission flags are coarser than the packaged ones.** `doctor`
   validates that the `{test_command}` placeholder expanded, not what the
   vendor's flag actually grants.

Points 2 through 4 only surface in a real run, so finish with one against a
throwaway repository before trusting a new agent with your own.

[`examples/`](examples/) has a folder per CLI — [claude](examples/claude/),
[codex](examples/codex/), [kiro](examples/kiro/) — each running every role on
that one vendor, with a table comparing how they map onto the knobs above and
notes on which of the four problems each one actually hit.

## Why command prefixes instead of SDKs?

The CLI boundary is what keeps this small:

- it uses the authentication you already have in each CLI;
- upgrading an agent CLI does not couple the orchestrator to an SDK;
- each role can have independent permissions/model flags;
- replacing one agent is just YAML;
- the Git worktree remains the shared protocol between agents.

The cost is that agent output is parsed as text rather than consumed as a
typed event stream. A typed SDK is the natural next step if that parsing ever
becomes the thing that breaks.

## Safety model

The important separation is:

- Claude architect/reviewer runs with `--disallowedTools "Edit Write
  NotebookEdit"` in the default config. The packaged reviewer additionally may
  execute only the effective test command, and it runs in the isolated
  worktree.
- The architect runs in your real repository, not in the worktree, so the
  packaged architect deliberately has no `{test_command}` grant. `doctor`
  warns if a custom config adds one because that grants project-command
  execution in the real checkout.
- Codex developer/fixer gets workspace write access in the isolated worktree.
- Git commit, destructive and publishing actions remain forbidden to agents by
  prompt.
- At a terminal result, the orchestrator creates a local commit on the run's
  own branch using the repository's identity, signing configuration and hooks.
- A fan-out run merges task branches only into its own integration branch. The
  orchestrator never merges into the user's original branch, rebases, pushes,
  deletes worktrees during a run, modifies remotes, or touches the
  branch/index/worktree in the user's original checkout.
- The final branch/worktree and its traceable commit are left for human
  inspection.

The agents' own CLI sandbox and permission settings remain the real enforcement
boundary; prompts are guidance, not a security boundary.

## Useful next additions

Shipped since this list was written: fan-out DAG execution, token accounting,
timeouts, retries,
persistent run state, `list`, `resume`, catchable-signal handling, capability
probes, empty-stage detection, and terminal commits on run branches. What is
still open, roughly in order of how much it would change the tool:

- **GitHub issue / PR as task input.**

Structured review output shipped: see
[Structured review findings](#structured-review-findings). Severity policy also shipped
as an [opt-in setting](#severity-policy-opt-in), off by default: no derived default
preserves current approval behavior in both directions.

## License

MIT. See [LICENSE](LICENSE).
