# Driving every role with opencode

Verified with read and write probes against **opencode 1.18.31**.

```sh
cp examples/opencode/opencode-stargate ~/.local/bin/   # anywhere on PATH
stargate --config examples/opencode/agents.yaml doctor --probe
stargate --config examples/opencode/agents.yaml run "your task"
```

The wrapper and `opencode` must both be on PATH. Stargate passes `{output}` as
the wrapper's first argument and appends the prompt last, where it becomes
opencode's positional message. The reader uses `--agent plan`, the built-in
read-only agent (`edit "*": deny`). That flag-selectable read-only mode is why
opencode was chosen over crush, which needs a config file for it. The writer
uses `--agent build --auto`; `--auto` approves permissions that are not
explicitly denied. Keep `--model` last: it takes one value. `--file` is an array
option and would swallow the appended prompt if it ended the command.

## Why a wrapper at all

opencode emits **nothing** when stdout is a regular file. That is exactly what
stargate's `core.py` `run_process()` supplies: an open trace file as stdout,
with stderr merged into it. In the verified experiment,
`opencode run ... > file 2>&1` produced zero bytes and had to be killed after
300s; the same command piped (`2>&1 | tail`) completed normally. The wrapper's
pipeline supplies the pipe that makes opencode produce output under stargate.

This is a different reason from kiro's wrapper, which handles its
`argv[0]`-relative sibling executable and the `> ` output marker.

## Auth uses the AI SDK variable names

Authentication uses the Vercel AI SDK name `GOOGLE_GENERATIVE_AI_API_KEY`,
not `GEMINI_API_KEY`. The wrapper maps `GEMINI_API_KEY` to
`GOOGLE_GENERATIVE_AI_API_KEY` when only the former is set.

## The model must be named and current

The config explicitly selects `google/gemini-3.6-flash`, which worked in the
verified probes. `opencode models` still lists the Gemini 2.5 family, but
Google rejects `gemini-2.5-flash` for new accounts.

## It is slow

Trivial prompts took **22s to 153s**, so the 120s default probe timeout flakes.
This example sets `probe_timeout_seconds: 420`, the value used for these
verified results against opencode 1.18.31:

| Agent | Capability | Result | Time |
|---|---|---|---|
| `opencode_reader` | read | OK | 138.6s |
| `opencode_writer` | write | OK | 111.2s |

`init-config` copies the agent blocks, **not** the example's settings. If you
select opencode through the wizard, also set `settings.probe_timeout_seconds`
to `420` in the generated config; otherwise it inherits the flaky 120s default.

## Known output limitation

The wrapper strips ANSI escapes, the `> <agent> · <model>` header and tool
marker lines (`→ Read`, `← Write`, `✱ Glob`). Tool **result** text such as
`Wrote file successfully.` still passes through mid-output. This is harmless
for verdict parsing: stargate's contract is the verdict on the **last** line,
which is clean.

## What this config gives up

opencode reports no token usage in this mode, so there is no `usage_pattern`
and `max_task_tokens` never fires for an all-opencode run.

The plan agent has no scoped `Bash({test_command})` equivalent, so there is no
separate reviewer test-command grant. Stargate still runs the configured tests
and pastes the report for the reviewer.
