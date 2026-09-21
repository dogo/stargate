# Driving every role with opencode

Verified with read and write probes against **opencode 1.18.31**.

```sh
cp examples/opencode/opencode-stargate ~/.local/bin/   # anywhere on PATH
stargate --config examples/opencode/agents.yaml doctor --probe
stargate --config examples/opencode/agents.yaml run "your task"
```

The wrapper, `opencode` and `python3` must be on PATH. Python is already a
stargate dependency; no `jq` installation is needed. Stargate passes `{output}` as
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
300s; the same command piped (`2>&1 | tail`) completed normally. This defect is
independent of the output format, so JSON mode still needs the FIFO.

The wrapper supplies a FIFO (named pipe) in a private temporary directory.
opencode writes into it as a background job; the wrapper filters the output,
then uses `wait` to preserve opencode's own failure status. If opencode succeeds,
a failure in the Python reader (including writing the answer) still makes the
wrapper exit nonzero. POSIX `sh` has no `pipefail`: a plain pipeline would report
only the reader's status even after an authentication or quota failure,
preventing stargate from retrying. On normal exit and on INT, TERM and
HUP, the wrapper reaps opencode, stopping it if necessary, and removes the FIFO
and directory. When the wrapper leads its process group (as it does under
stargate's `run_process()`), cleanup also sends TERM to that group to stop
opencode's Bun children. Otherwise it signals only opencode, to avoid signalling
the caller's group; descendants can survive a signal sent only to the wrapper.

SIGKILL cannot be trapped. The wrapper deliberately keeps opencode in its group
so stargate's whole-group SIGKILL reaches it too. A `doctor --probe` timeout,
however, kills **only the wrapper**: opencode can outlive it and keep billing.
Raising the probe timeout reduces the chance of hitting this unresolved path;
it does not provide cleanup after SIGKILL. See the timeout setting below.

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

## Latency depends on the provider's throttling

Observed round trips for trivial prompts range from about **12s to 153s**.
The high end was measured while the Gemini free tier was throttling, with
rate limiting and backoff. After the quota reset, a clean probe returned
`opencode_reader OK [14.5s]` and `opencode_writer OK [12.1s]`.

This example sets `probe_timeout_seconds: 420` as headroom for that throttling,
which exceeded the 120s default, rather than as a claim about opencode's own
speed. The example keeps the packaged **1800s** work timeout and only raises
the probe timeout. These verified results against opencode 1.18.31 used the
raised timeout on the throttled tier:

| Agent | Capability | Result | Time |
|---|---|---|---|
| `opencode_reader` | read | OK | 138.6s |
| `opencode_writer` | write | OK | 111.2s |

`init-config` copies the agent blocks, **not** the example's settings. A
wizard-generated config inherits the packaged **120s** probe timeout. Opencode
can exceed it under provider throttling: the measured 138.6s read probe above
exceeded that default, while the 111.2s write probe approached it.

The generated file already carries these lines in its commented settings block
at the bottom (with other settings between them):

```yaml
# settings:
#   probe_timeout_seconds: 120
```

Uncomment `settings:` and `probe_timeout_seconds`, and raise the latter to `420`:

```yaml
settings:
  probe_timeout_seconds: 420
```

This gives throttled probes more time before the timeout path where the wrapper
cannot clean up opencode.

## Output

Both agent commands supply `--format json`, which the wrapper requires. Update
or reinstall the wrapper and config together. The adapter consumes opencode's
newline-delimited JSON event stream and writes the latest assistant response
to `{output}` and stdout. This event
shape was verified with opencode 1.18.31:
text is in `part.text` when `part.type` is `text`; tool use has its own event
type, so tool results never reach the answer file. When valid text follows a
tool event, it replaces earlier narration, preventing that narration from
prefixing a JSON review. A trailing tool event preserves the preceding answer.
If a text part is re-emitted
as it grows, the reader keeps the last value for its `part.id`, concatenating
the remaining parts in first-appearance order. Text parts without a string ID
share one fallback key and are treated as a single growing part.

Lines that are not JSON objects go to stderr, which stargate includes in its
run trace, rather than into the answer. Input and output use UTF-8 regardless
of the locale; undecodable diagnostic bytes are escaped on stderr and skipped.
Other event types are ignored. If no
text parts arrive (for example, a custom config omits `--format json`), the
answer is empty and stargate reports that the agent declares `{output}` but
wrote nothing.

## What this config gives up

`--agent build --auto` auto-approves permissions that are not explicitly denied,
including shell execution inside the run's worktree. This is broader than
Claude's `acceptEdits` and Gemini's `auto_edit`, which auto-approve edits only.

opencode reports no token usage in this mode, so there is no `usage_pattern`
and `max_task_tokens` never fires for an all-opencode run.

The plan agent has no scoped `Bash({test_command})` equivalent, so there is no
separate reviewer test-command grant. Stargate still runs the configured tests
and pastes the report for the reviewer.
