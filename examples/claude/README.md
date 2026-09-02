# Every role on Claude Code

Verified against **Claude Code 2.1.236**: both probes pass, read and write.

```sh
stargate --config examples/claude/agents.yaml doctor --probe
```

`claude -p --output-format text` prints only the final message, so no `{output}`
wrapper is needed — stdout *is* the answer. That is why the packaged config
runs the Claude roles bare.

## `--disallowedTools` will eat your prompt

This one is load-bearing and easy to miss. The flag is variadic:

```
--disallowedTools, --disallowed-tools <tools...>
```

Stargate appends the prompt as the last argument, so if `--disallowedTools` is
the last option in the command, it swallows the prompt as another tool name and
claude dies with:

```
Error: Input must be provided either through stdin or as a prompt argument when using --print
```

Both the packaged config and this one end with `--model`, which terminates the
variadic. Keep a non-variadic option last in any Claude command you write.

## The Claude roles report no tokens

The packaged config sets no `usage_pattern` for Claude, because text mode prints
no usage. That is a real gap: in the default mixed setup `max_task_tokens` only
counts the Codex half of a run.

`--output-format json` carries the answer and the usage together, which is what
[`claude-json-stargate`](claude-json-stargate) unpacks — JSON stays on stdout
for `usage_pattern`, `.result` goes to `{output}`:

```yaml
claude_reader:
  command:
    - claude-json-stargate
    - "{output}"
    - -p
    - --disallowedTools
    - Edit Write NotebookEdit
    - --model
    - opus
  usage_pattern: '"output_tokens":(\d+)'
```

Note what that number is and is not. `output_tokens` is the only clean integer
in the payload; input and cache tokens usually dominate a real run, so the
budget undercounts badly. There is no single total-tokens field.

Do not reach for `total_cost_usd` instead. `parse_usage` strips the decimal
point, so the scale depends on how many digits the value happens to have:
`0.0022986` becomes `22986` while `0.5` becomes `5`. Verified, and not usable
as a budget.

For a real in-flight cap on Claude, use its own flag: `--max-budget-usd`.
`max_task_tokens` is only checked between phases anyway.
