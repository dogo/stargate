# CLAUDE.md

## Bootstrap: verify the graph daemon + hook (run FIRST, once per session)

The knowledge graph only stays fresh if (a) the machine's `crg-watch` daemon is
watching THIS repo and (b) this repo has the post-commit hook. The daemon is
global but watches only repos explicitly added. Run this idempotent guard at the
start of a session before relying on the graph; it installs/activates whatever is
missing and is a no-op when everything is already set up:

```sh
repo="$(git rev-parse --show-toplevel)"
# Hook + MCP config (creates .git/hooks/post-commit if absent):
[ -x "$repo/.git/hooks/post-commit" ] || code-review-graph install -y
# Daemon running:
code-review-graph daemon status >/dev/null 2>&1 || code-review-graph daemon start
# This repo watched by the daemon:
code-review-graph daemon status 2>/dev/null | grep -qF "$repo" \
  || code-review-graph daemon add "$repo"
```

If `code-review-graph` is not on PATH, it lives at `$HOME/.local/bin/code-review-graph`.


This project keeps a single source of agent guidance, in `AGENTS.md`.
Do not duplicate content here; edit `AGENTS.md`.

@AGENTS.md
