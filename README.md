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

Config lookup, first hit wins:

1. `--config <path>`
2. `./.stargate.yaml` in the repo you are standing in
3. `~/.config/stargate/agents.yaml` (or `$XDG_CONFIG_HOME`)
4. the `agents.yaml` packaged with the install

`stargate doctor` prints which one it picked. Per-project overrides use
`.stargate.yaml`, not `agents.yaml`, so a global install can't accidentally
read an unrelated repo's `agents.yaml`.

## Check setup

```bash
stargate doctor
```

This only checks that `git`, `claude`, and `codex` are on `PATH`; it makes no
external calls. To also verify authentication, credits, quota, and model
availability, explicitly run the potentially billable probes:

```bash
stargate doctor --probe
```

Each unique agent command is called once, even when several roles use it.
Results include `OK` or `FAIL`, elapsed time, and the CLI's error output on
failure. Any failed probe makes `doctor` exit non-zero. The vendor-specific
cheap prompt stays in config:

```yaml
architect:
  command: [claude, -p, --output-format, text]
  probe: "Reply with exactly OK."
```

Agents without `probe` are skipped, so existing user configs remain usable.

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
├── config.yaml        # the effective config, frozen at run start
├── prompts/           # the four prompts, frozen at run start
├── plan.md
├── plan.md.log        # the agent's full trace, written live
├── developer.txt
├── developer.txt.log
├── review-1.md
├── fix-1.txt          # only when needed
├── tests-*.txt        # if test_command is configured
└── summary.md
```

Each role prints its `.log` path before starting, so a silent multi-minute
agent can be followed with `tail -f`, and its exit code and duration when it
ends. The trace is not echoed to the terminal.

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
stargate resume <run-id> --config ./fixed.yaml
```

The review loop always restarts from its first attempt: re-reviewing is
idempotent and cheap next to re-implementing.

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

- Claude architect/reviewer uses plan/read-oriented mode in the default config.
- Codex developer/fixer gets workspace write access in the isolated worktree.
- Git destructive/publishing actions are forbidden by prompt.
- The orchestrator does not auto-merge or auto-push.
- The final branch/worktree is left for human inspection.

The agents' own CLI sandbox and permission settings remain the real enforcement
boundary; prompts are guidance, not a security boundary.

## Useful next additions

A v2 can add:

- parallel agents per module via one worktree per task;
- a `tasks.json` produced by the architect;
- dependency graph / DAG scheduling;
- structured JSON review output;
- automatic language/project detection for test commands;
- GitHub issue / PR input;
- cost/token accounting;
- retry/timeouts;
- persistent run state and resume;
- a final human approval gate before commit/merge;
- MCP-shared project context.

## License

MIT. See [LICENSE](LICENSE).
