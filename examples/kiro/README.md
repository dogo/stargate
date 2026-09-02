# Driving every role with kiro-cli

Verified end to end against **kiro-cli 2.21.0** on macOS: a full `stargate run`
reached `Verdict: APPROVED` with all four roles on kiro-cli.

```sh
cp examples/kiro/kiro-stargate ~/.local/bin/   # anywhere on PATH
stargate --config examples/kiro/agents.yaml doctor --probe
stargate --config examples/kiro/agents.yaml run "your task"
```

Stargate itself needed no change. The wrapper exists because of three things
kiro-cli does that the packaged Claude and Codex commands do not.

## Why a wrapper at all

**`chat` cannot be reached through a symlink.** It execs a sibling
`kiro-cli-chat` binary resolved from `argv[0]`'s directory, so calling the
Homebrew symlink fails with `error: No such file or directory (os error 2)`.
The wrapper calls the binary inside the app bundle; override the path with
`KIRO_BIN` if yours lives elsewhere.

**Its stdout is not the final message.** Even under `NO_COLOR=1` the reply
carries ANSI escapes and a `> ` marker on the first line:

```
^[[m> ^[[0mline one^[[0m^[[0m
line two^[[0m^[[0m
VERDICT: APPROVED
```

Stargate reads the reviewer's verdict as the exact last line, so the wrapper
strips both and writes the clean text to `{output}`. kiro-cli has no
`--output-last-message` equivalent of its own.

The `▸ Credits: N • Time: Ns` footer goes to *stderr*, which stargate merges
into the run log. Leaving it there is deliberate: that is how `usage_pattern`
still sees it without it landing in `{output}`.

## Credits are not tokens

kiro-cli reports fractional credits. A `([\d,]+)` pattern captures the `0` of
`Credits: 0.21` and the budget silently counts nothing — a run costing 0.53
credits reported `Tokens reported: 0`.

Capturing the dot works because `parse_usage` strips it, turning `0.21` into
`21`. The budget then counts hundredths of a credit, so set `max_task_tokens`
in that same unit or leave it at `0`.

## What this config gives up

The packaged Claude reviewer gets `--allowedTools "Bash({test_command})"`:
execution scoped to one exact command. kiro-cli's `--trust-tools` grants by
*category*, with no per-command scoping, so a reviewer that needs to run tests
would get all of bash.

This config therefore gives the reader roles `--trust-tools=read,grep` and no
test grant at all. Stargate still runs the test command itself and pastes the
report; the reviewer just cannot re-run it to verify. `doctor` will not warn
about this, because it validates the placeholder, not the vendor's flag.
