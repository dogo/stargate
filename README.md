# Stargate

A deliberately small local orchestrator that lets **Claude Code** and the
**Codex CLI** collaborate on the same software task without concurrently
editing the same checkout.

Default flow:

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
Claude / reviewer
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

Every terminal result with changes is committed on the run's own branch,
including a reviewer that still requests changes, a failing test command, or a
token-budget stop. The verdict is part of the commit message. A run that
crashes does not commit; `resume` does so when the run eventually reaches a
terminal result.

The orchestrator never merges, rebases, pushes, deletes the worktree, or
touches the branch checked out in the user's original repository. The agents
themselves remain forbidden to commit.

## Requirements

- Python 3.10+
- Git
- Claude Code installed and authenticated
- Codex CLI installed and authenticated

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
| `commit` | `true` | Commit a terminal result on the run's own branch. Set to `false`, or pass `--no-commit` to `run` or `resume`, to leave the worktree dirty as older versions did. |
| `max_review_loops` | `2` | Fixer passes allowed after the first review. `0` reviews once and stops. Overridable per run with `--max-review-loops`. |
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
| `1` | The run failed — an agent errored, timed out, or config was invalid. `resume` is offered. |
| `2` | The reviewer still requested changes after the last allowed fixer pass, or the fixer changed nothing and the loop stopped early. |
| `3` | Approved, but the explicit test command or an `auto`-detected command failed. |
| `4` | `max_task_tokens` was reached; the run stopped between phases. |
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
and agents, each role's resolved command, the prompt file each role would use,
and the configured or detected project test commands with their evidence. It
makes no external calls, so `FOUND` means only that the executable is on `PATH`
— see
[Probing agents](#probing-agents) to actually verify that an agent can run.

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

The packaged architect prompt asks for a `NAME:` first line. Stargate strips a
valid line before forwarding the plan to the developer, reviewer, and fixer. A
custom/older prompt that omits it, or a malformed name, safely falls back to the
task slug and leaves the plan untouched.

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
├── state.json         # stage, status, commit, exclusions -- what `resume` reads
├── config.yaml        # the fully merged effective config, frozen at run start
├── prompts/           # the four prompts, frozen at run start
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

The four prompts are what you actually tune. Copy them somewhere writable:

```bash
stargate init-prompts        # writes ~/.config/stargate/prompts/*.md
```

Then delete the ones you don't want to change. Lookup is per-file, first hit
wins:

1. `settings.prompts_dir` from the active config, if set
2. `~/.config/stargate/prompts/`
3. the prompts packaged with the install

So keeping only a custom `reviewer.md` leaves the other three on the defaults.
`stargate doctor` prints the file each role resolved to.

The packaged `architect.md` also asks the architect to begin its response with
`NAME: <two to four words>`. Stargate treats that line as optional for custom
and frozen prompts: only a valid first non-empty `NAME:` line is removed, and
the rest of the plan is forwarded unchanged.

Prompts committed with a project:

```yaml
# .stargate.yaml
settings:
  prompts_dir: .stargate-prompts   # relative to the repo you run in
```

Two things the prompt templates have to respect:

- Only known placeholders are substituted, by literal replacement: `{task}` and
  `{base_ref}` everywhere, plus `{plan}` (developer, reviewer, fixer),
  `{tests}` (reviewer, fixer) and `{review}` (fixer). Every other brace is left
  alone, so a prompt may contain JSON, CSS or an f-string example verbatim.
  These are separate from the agent-command placeholders described below.
- `reviewer.md` is a contract with the orchestrator: the model's last line has
  to be exactly `VERDICT: APPROVED` or `VERDICT: CHANGES_REQUESTED`. Anything
  else aborts the run.

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
narrow: Git advances only the run branch inside the run worktree. Use
`settings.commit: false` or `--no-commit` when that tradeoff is not wanted.

Stargate commits only after a terminal verdict, never after individual fixer
passes. A normal run therefore produces one commit. A resume of already
committed work finds an empty index and produces no second commit; a later
`--redo` that genuinely adds work can produce a new terminal commit. Empty
commits are never created.

Red outcomes are durable too. `CHANGES_REQUESTED`, a failed test command and
`BUDGET_EXCEEDED` are recorded in the subject and
`Stargate-Verdict` trailer rather than being presented as success. Crashes,
agent errors, invalid reviewer output and catchable signals do not reach the
finish path, so they remain uncommitted until a successful `resume` reaches a
verdict.

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
verdict plus `commit_error`, and the terminal names the intact worktree and a
manual `git commit` recovery command. A hook that succeeds but rewrites files
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

  RUN ID                                      STATUS            STAGE      UPDATED             TASK
* 20260831-101304-add-passkey-authentication  failed            developer  2026-08-31T10:19:42 Add passkey authentication
    branch    stargate/add-passkey-authentication-20260831-101304
    worktree  /home/me/.stargate-worktrees/project/20260831-101304-add-passkey-authentication  (MISSING)
  20260830-181500-update-docs                 approved          review     2026-08-30T18:22:11 Update docs
    branch    stargate/update-docs-20260830-181500
    worktree  /home/me/.stargate-worktrees/project/20260830-181500-update-docs

* resumable. Resume the newest with: stargate resume 20260831-101304-add-passkey-authentication
```

The former `stargate runs` spelling remains an alias.

Failed runs and rows still reading `running` are marked with `*`. A `running`
row normally means the run is still in flight, but it can also mean the process
was stopped by an uncatchable hard kill such as SIGKILL or power loss. Missing
or corrupt `state.json` files are shown as unknown rather than crashing or
hiding the run. If `.stargate/runs/` does not exist or is empty, the command
reports that there are no runs. Listing only reads recorded state: it never
creates, repairs or deletes run artifacts.

## Cleaning runs

`stargate clean <run-id>` removes one run; `stargate clean --all` attempts every
recorded run. A run is removed only when its branch is merged into the current
`HEAD` and Git accepts the worktree as clean. There is no force option. After
those checks, Stargate removes the linked worktree, its `stargate/*` branch and
the run's `.stargate/runs/<run-id>/` artifacts. A missing worktree is pruned
through Git before the remaining branch and artifacts are removed.

## Resuming a failed run

Every stage is recorded in the run's `state.json` before and after it runs. If
a stage fails — the CLI is out of credits, the machine sleeps, a package
upgrade removes the install mid-run — the run stops and prints:

```text
Resume with: stargate resume 20260831-101304-add-passkey-authentication
```

`resume` reuses the plan, branch, worktree, frozen config and frozen prompts,
and skips the stages already marked complete. It does not produce a second
plan, branch or worktree. A developer that exited successfully but changed
nothing is deliberately not marked complete, so plain `resume` reruns it.
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

To rerun a stage that was already recorded as complete, use the supported
`--redo` option instead of editing `state.json`:

```bash
stargate resume <run-id> --redo developer
```

`--redo` accepts `architect` or `developer` and is repeatable. Redoing only the
architect leaves the developer marked complete, which means the new plan would
not be implemented; stargate warns in that case, and you can pass both
`--redo architect --redo developer`.

The review loop always restarts from its first attempt: re-reviewing is
idempotent and cheap next to re-implementing.

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
budget stops the *next* agent from starting, never the one already running — a
single runaway invocation still overshoots. Hitting the cap ends the run with
verdict `BUDGET_EXCEEDED` and exit code 4, leaving the branch and worktree
intact and committing any work produced so far. No empty commit is made when
the budget is reached before implementation changes exist.

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

Every agent is just a command prefix. For example, swapping the reviewer to
Codex only requires another agent definition plus:

```yaml
workflow:
  reviewer: codex_reviewer
```

Likewise you can invert the default design and make Codex the architect and
Claude the implementer.

## Why command prefixes instead of SDKs?

For a POC, the CLI boundary is useful:

- it uses the authentication you already have in each CLI;
- upgrading either vendor does not couple the orchestrator to an SDK;
- each role can have independent permissions/model flags;
- replacing one agent is just YAML;
- the Git worktree remains the shared protocol between agents.

For a production-grade version, a typed SDK/JSON event stream is the natural
next step.

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
- The orchestrator does not merge, rebase, push, delete the worktree, modify
  remotes, or touch the branch/index/worktree in the user's original checkout.
- The final branch/worktree and its traceable commit are left for human
  inspection.

The agents' own CLI sandbox and permission settings remain the real enforcement
boundary; prompts are guidance, not a security boundary.

## Useful next additions

Shipped since this list was written: token accounting, timeouts, retries,
persistent run state, `list`, `resume`, catchable-signal handling, capability
probes, empty-stage detection, and terminal commits on run branches. What is
still open, roughly in order of how much it would change the tool:

- **Fan-out.** A `tasks.json` produced by the architect, one worktree per task,
  DAG scheduling, then an integrated review across the branches. This is the
  real v2 and everything else is small next to it.
- **Structured review output** (JSON findings instead of a prose verdict).
- **GitHub issue / PR as task input.**

## License

MIT. See [LICENSE](LICENSE).
