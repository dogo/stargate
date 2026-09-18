# Every role on the Gemini CLI

Verified against **gemini-cli 0.60.0**: both probes pass, read and write.
Recorded results from `stargate doctor --probe`: `gemini_reader (read)` OK,
`gemini_writer (write)` OK.

```sh
stargate --config examples/gemini/agents.yaml doctor --probe
```

`gemini --output-format text` prints the answer on stdout, like
`claude -p --output-format text`. No `{output}` placeholder or wrapper is needed.

## Keep `--prompt` last

Stargate appends the prompt as the last argument. `--prompt` takes that prompt
as its value and is not variadic, so both commands end with it.

These options are arrays and would swallow the appended prompt if placed last:
`--policy`, `--admin-policy`, `--include-directories`, `--extensions`,
`--allowed-tools`, and `--allowed-mcp-server-names`. This is the same trap as
Claude's `--disallowedTools`; any additional options must go before `--prompt`.

## Approval modes map onto the roles

Architect and reviewer use `--approval-mode plan`, Gemini's read-only mode.
Unlike Claude Code's `--permission-mode plan`, it answers on stdout and does
not divert the answer to a plan file.

Developer and fixer use `--approval-mode auto_edit`: edit tools are
auto-approved, everything else still prompts, and headless a prompt is a
denial. This is the same reasoning as Claude's `acceptEdits`. Do not use `yolo`.

`--skip-trust` is required on both blocks. A run's worktree is always a fresh
directory outside the repository and is never trusted. Without this option,
Gemini prints:

```text
Approval mode overridden to "default" because the current folder is not trusted
```

That downgrade silently removes the writer's ability to edit.

`--allowed-tools` is marked `[DEPRECATED]` in 0.60.0. It only bypasses the
confirmation dialog; it restricts nothing and must not be used for read-only
roles.

There is no `reviewer_command`. Plan mode forbids shell execution, so the
reviewer cannot receive a scoped `Bash({test_command})` grant like Claude's.
It trusts the pasted test report because the vendor offers no narrower option;
stargate runs the test command itself.

## Export the API key

Authentication uses `GEMINI_API_KEY` from Google AI Studio's free tier. Export
the variable in the shell that launches stargate; stargate passes its
environment through. Gemini's own `~/.gemini/.env` auto-loading did not apply
when it was launched from an unrelated working directory, so exporting the
variable is the reliable path.

## Text mode reports no tokens

Neither block has a `usage_pattern`: Gemini reports no token usage under
`--output-format text`, exactly like the Claude text-mode entries.
Consequently, `max_task_tokens` cannot account for Gemini usage in this setup.
