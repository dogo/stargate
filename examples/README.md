# Worked examples

One folder per agent CLI. Each holds a config running **every** role on that
one vendor, plus notes on what that CLI does that stargate has to be told
about. The packaged `stargate/agents.yaml` is the mixed Claude + Codex default;
these are the single-vendor versions and the evidence behind them.

Every config here was verified with `doctor --probe`, which exercises real
reads and writes rather than checking that a binary exists:

```sh
stargate --config examples/<vendor>/agents.yaml doctor --probe
```

| | [claude](claude/) | [codex](codex/) | [kiro](kiro/) |
|---|---|---|---|
| Version verified | 2.1.236 | 0.152.0 | 2.21.0 |
| Needs `{output}` | no | yes | yes |
| Has its own flag for it | — | `--output-last-message` | none, needs a wrapper |
| Wrapper required | no | no | **yes** |
| Reports usage | only as JSON | `tokens used N` | `Credits: N.NN` |
| `usage_pattern` works out of the box | no | yes | no |
| Read-only role | `--disallowedTools` | `--sandbox read-only` | `--trust-tools=read,grep` |
| Scoped `{test_command}` grant | yes, `Bash(...)` | no, sandbox not allow-list | no, category only |

## The four things that go wrong

In the order you find out, which is not the order of severity:

1. **Not on PATH, or the prompt is not the last argument.** `doctor` says so
   at once. Claude's variadic `--disallowedTools` is a live version of this: it
   swallows the prompt unless another option follows it.
2. **stdout is a session trace, not the answer.** Then the command needs
   `{output}`. Codex has a flag; kiro needs a wrapper.
3. **`usage_pattern` matches nothing, or the wrong number.** Silent. The run
   succeeds and reports `Tokens reported: 0`, so `max_task_tokens` never fires.
   Kiro's fractional credits and Claude's missing pattern are both this.
4. **Permission flags are coarser than the packaged ones.** `doctor` checks
   that the `{test_command}` placeholder expanded, never what the vendor's flag
   actually grants.

Only the first is caught by `doctor`. Finish a new adapter with a real run
against a throwaway repository.
