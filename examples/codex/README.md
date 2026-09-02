# Every role on the Codex CLI

Verified against **codex-cli 0.152.0**: both probes pass, read and write.

```sh
stargate --config examples/codex/agents.yaml doctor --probe
```

## Every role needs `{output}`

`codex exec` streams the whole session to stdout — hook lines, the reply, then
a token footer:

```
hook: SessionStart Completed
codex
pong
tokens used
7,875
```

Forward that as `{plan}` and the next role pays for the previous role's trace.
Worse, the reviewer's `VERDICT:` line would no longer be last.

Codex has its own flag for this, so no wrapper is needed:

```yaml
command: [codex, exec, --sandbox, read-only, --output-last-message, "{output}"]
```

The footer stays on stdout, which is exactly where `usage_pattern` reads it:
`'tokens used\s+([\d,]+)'` matches across the newline and yields `7875`.

## Sandbox modes map onto the roles

`--sandbox` takes `read-only`, `workspace-write`, or `danger-full-access`. The
first two are the reader and writer roles here. Unlike Claude's
`--allowedTools`, this is a sandbox rather than a tool allow-list, so there is
no per-command grant to hand `{test_command}` to — the reader roles get no test
grant, and stargate runs the test command itself.

## It refuses to run outside a git repository

Not a problem for a run — stargate always invokes agents inside the worktree.
It matters for probes, which run in a throwaway directory; `doctor` does a
`git init` there for exactly this reason.
