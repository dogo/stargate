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
 ├── APPROVED ─────────────► done
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

The orchestrator itself never commits, merges, rebases, pushes, or deletes the
worktree.

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
`command`, `probe`, `usage_pattern` and `env` as needed; entries for all other
agents are inherited. Top-level scalar values use the most-specific value.

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
| `test_command` | `""` | Shell command run in the worktree after the developer and after every fixer pass. Empty means no tests. |
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
`usage_pattern`, `env`.

## Exit codes

| code | meaning |
|---|---|
| `0` | Approved, and the test command passed or was not configured. |
| `1` | The run failed — an agent errored, timed out, or config was invalid. `resume` is offered. |
| `2` | The reviewer still requested changes after the last allowed fixer pass. |
| `3` | Approved, but the test command failed. |
| `4` | `max_task_tokens` was reached; the run stopped between phases. |
| `129` | The run received SIGHUP. Its state is recorded as failed and `resume` is offered. |
| `130` | The run was interrupted with Ctrl-C/SIGINT. Its state is recorded as failed and `resume` is offered. |
| `143` | The run received SIGTERM. Its state is recorded as failed and `resume` is offered. |

`doctor` exits `1` when a binary is missing, a prompt cannot be resolved, or a
`--probe` call fails.

## Check setup

```bash
stargate doctor
```

It prints the numbered config layers and provenance of the effective settings
and agents, each role's resolved command, and the prompt file each role would
use. It makes no external calls, so `FOUND` means only that the executable is on
`PATH` — see
[Probing agents](#probing-agents) to actually verify that an agent can run.

## Probing agents

`doctor` alone never calls an agent. `doctor --probe` makes one real,
**billable** call per distinct agent command — two for the four default roles,
since they map onto two commands:

```console
$ stargate doctor --probe
Agent probes:
  FAIL architect, reviewer [4.3s]
       Credit balance is too low
  OK   developer, fixer [7.9s]
```

The prompt lives in the config, so the orchestrator stays vendor-agnostic:

```yaml
architect:
  command: [claude, -p, --output-format, text, --model, opus]
  probe: "Reply with exactly OK."
```

Because the probe runs the agent's *real* command, it catches a wrong model
name or an unsupported flag too, not only credentials — including an agent that
exits 0 while writing nothing to its `{output}` file. Agents with no `probe`
key report `SKIP` and do not fail the exit code. Probes run in a throwaway git
repository, never in yours, and use `probe_timeout_seconds` (120) rather than
the much longer agent timeout.

## Use it against a repository

Stand in the repository you want the agents to modify:

```bash
cd ~/dev/my-project
stargate run "Add pagination to the users endpoint"
```

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

## What gets created

Suppose the task is `Add passkey authentication`.

The implementation gets its own branch similar to:

```text
stargate/add-passkey-authentication-20260830-181500
```

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
├── state.json         # stage, status, error, tokens -- what `resume` reads
├── config.yaml        # the fully merged effective config, frozen at run start
├── prompts/           # the four prompts, frozen at run start
├── plan.md
├── plan.md.log        # the agent's full trace, written live
├── plan.md.attempt-2.log  # later attempts keep separate traces
├── developer.txt
├── developer.txt.log
├── review-1.md
├── fix-1.txt          # only when needed
├── tests-*.txt        # if test_command is configured
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

The test command is per-project, so it belongs in the repo's `.stargate.yaml`:

```yaml
settings:
  test_command: "pytest -q"
```

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
is a blocking review finding rather than a number nobody reads.

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

Prompts committed with a project:

```yaml
# .stargate.yaml
settings:
  prompts_dir: .stargate-prompts   # relative to the repo you run in
```

Two things the templates have to respect:

- Only known placeholders are substituted, by literal replacement: `{task}` and
  `{base_ref}` everywhere, plus `{plan}` (developer, reviewer, fixer),
  `{tests}` (reviewer, fixer) and `{review}` (fixer). Every other brace is left
  alone, so a prompt may contain JSON, CSS or an f-string example verbatim.
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

`stargate runs` shows the runs recorded in the current repository, newest
first, without requiring an active config:

```console
$ stargate runs
Runs in /home/me/project (newest first):

  RUN ID                                      STATUS            STAGE      UPDATED             TASK
* 20260831-101304-add-passkey-authentication  failed            developer  2026-08-31T10:19:42 Add passkey authentication
  20260830-181500-update-docs                 approved          review     2026-08-30T18:22:11 Update docs

* resumable. Resume the newest with: stargate resume 20260831-101304-add-passkey-authentication
```

Failed runs and rows still reading `running` are marked with `*`. A `running`
row normally means the run is still in flight, but it can also mean the process
was stopped by an uncatchable hard kill such as SIGKILL or power loss. Missing
or corrupt `state.json` files are shown as unknown rather than crashing or
hiding the run. If `.stargate/runs/` does not exist or is empty, the command
reports that there are no runs. Listing is read-only: it never creates, repairs
or deletes run artifacts.

## Resuming a failed run

Every stage is recorded in the run's `state.json` before and after it runs. If
a stage fails — the CLI is out of credits, the machine sleeps, a package
upgrade removes the install mid-run — the run stops and prints:

```text
Resume with: stargate resume 20260831-101304-add-passkey-authentication
```

`resume` reuses the plan, branch, worktree, frozen config and frozen prompts,
and skips the stages already marked complete. It does not produce a second
plan, branch or worktree.

By default it runs under the config frozen into the run, so resuming cannot
silently change the agents the earlier stages ran under. Pass `--config` to
override that, which is how you resume past a broken agent definition:

```bash
stargate --config ./fixed.yaml resume <run-id>
```

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
intact.

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
  NotebookEdit"` in the default config. This matters: the architect runs in
  your real repository, not in the worktree.
- Codex developer/fixer gets workspace write access in the isolated worktree.
- Git destructive/publishing actions are forbidden by prompt.
- The orchestrator does not auto-merge or auto-push.
- The final branch/worktree is left for human inspection.

The agents' own CLI sandbox and permission settings remain the real enforcement
boundary; prompts are guidance, not a security boundary.

## Useful next additions

Shipped since this list was written: token accounting, timeouts, retries,
persistent run state, `runs`, `resume`, and catchable-signal handling. What is
still open, roughly in order of how much it would change the tool:

- **Fan-out.** A `tasks.json` produced by the architect, one worktree per task,
  DAG scheduling, then an integrated review across the branches. This is the
  real v2 and everything else is small next to it.
- **Output validation.** Three separate integration bugs have been the same
  shape: an agent's answer was not what it printed. The orchestrator still
  trusts whatever it receives — an empty, truncated or summarised plan is
  forwarded without complaint.
- **Structured review output** (JSON findings instead of a prose verdict).
- **Project detection** for `test_command`.
- **GitHub issue / PR as task input.**

## License

MIT. See [LICENSE](LICENSE).
