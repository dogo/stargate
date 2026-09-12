# Task sources and pull requests

Design notes. No code in this document.

Two independent deliveries, **in this order**:

1. **Read the task from a tracker.** Nothing leaves the machine.
2. **Open a pull request when the run ends.** The first thing stargate would ever do with a
   remote, irreversible effect.

Separate on purpose: only the second crosses an outward-facing boundary, and bundling them
would make the easy half wait for the hard half's argument.

## The mechanism, shared by both

The product already solved this problem once: **an agent is a command prefix, not an SDK.**
The same move works in both directions.

- **In:** a command that exits 0 and prints the task as text on stdout.
- **Out:** a command that receives the branch and title, and the body on stdin.

Stargate never learns what GitHub is. No auth, API version, rate limit, pagination or response
format enters the code — so none of it breaks when a vendor changes something. `gh`, `glab`,
`curl` against a Gitea, a company's internal CLI, an `ssh` to another machine: identical from
its side.

The cost does not disappear, it changes owner: whoever writes the config is responsible for
producing plain text. If the source returns JSON, `jq` is their problem.

```yaml
task_sources:
  - hosts: [github.com]
    commands:
      - [gh, issue, view, "{url}", --json, title,body, --jq, '.title + "\n\n" + .body']
      - [fetch-github-issue, "{url}"]      # a wrapper, if a fallback is wanted
  - hosts: [gitlab.internal.example]
    commands:
      - [curl, -sS, -H, "PRIVATE-TOKEN: $GL_TOKEN", "{url}"]

pull_request:
  command: [gh, pr, create, --head, "{branch}", --title, "{title}", --body-file, "-"]
```

---

# Delivery 1: read the task

```bash
stargate run --from https://github.com/owner/repo/issues/42
```

1. Match the URL's **host** against `task_sources` and pick that entry's commands. An
   unconfigured host is an error naming the host, not an attempt.
2. Run that entry's commands **in order**; the first that exits 0 with non-empty output wins.
   If all fail, the error names each attempt with its own stderr.
3. Empty or whitespace-only output is an error **before any agent is called**.
4. The text becomes the task, exactly as if it had been typed. Provenance is recorded in
   `state.json` and the summary.
5. The branch is named from the ref: `stargate/42-<slug>` rather than the slug of the task's
   first sentence.
6. From there it is an ordinary run.

## Decisions

D1. **A task source is a command, not an integration.** Same shape as agents: a command prefix
plus placeholders, never a client library.

D2. **Stargate never learns what GitHub is.** Nothing vendor-specific in the code means nothing
vendor-specific to maintain when a vendor changes.

D3. **Falling back from a CLI to an API is not a new mode, it is another command.** There is no
"API mode"; there is a list of commands tried in order, and `curl` is a command like any other.

D4. **A list of commands, not `sh -c 'a || b'`.** The shell already does fallback with `||` and
would work today with no code at all — but `doctor.py:242-246` checks the **first element** of
each command against PATH, and with `sh -c` the binary is `/bin/sh`, always present. The
one-liner would hide the dependency from the one tool that exists to expose dependencies. The
list also gives a diagnosable error instead of an `sh` that exited 1.

D5. **Auth reuses what exists.** Entries already accept `env:` per entry, including
`{VAR: null}` to **remove** a variable. No new concept.

D6. **`jq` lives in the config.** Stargate does not extract fields from JSON. This is the line
where "vendor-agnostic" would be crossed without noticing.

D7. **The ref is a URL from a git interface** — GitHub, GitLab, Bitbucket, whatever — and
authentication belongs to whoever configures it. Public needs no credential, private does, and
stargate cannot tell the difference: to it, this is a command that prints text.

D8. **The source is chosen by matching the URL's host**, not by knowing the vendor. The config
declares which hosts each entry serves. Better than a `--from gh:42` selector, which would make
the person repeat what the URL already says.

D9. **An unconfigured host is an error, not an attempt.** Stargate does no generic fetching of
its own: a web UI URL returns HTML, and extracting an issue from that would require knowing the
vendor's format — exactly what D6 refuses.

D10. **The only placeholder is `{url}`, plus `{host}` and `{path}` for convenience.** Stargate
substitutes what it can read off the URL without interpreting it, and nothing else.

This has a consequence worth stating, because otherwise an implementer will invent something: a
REST fallback usually needs a *different* URL than the web one — GitHub's is
`api.github.com/repos/OWNER/REPO/issues/N`, whose shape is not derivable from
`github.com/OWNER/REPO/issues/N` without knowing GitHub. Stargate will not do that surgery.
Whoever needs it puts it in a small wrapper script and lists the script as the command, which
keeps `doctor` able to see the real dependency (D4) instead of hiding it behind `sh -c`.

D11. **Whoever points at a source owns what is in it.** No confirmation gate. The person asked
to read that URL; knowing what it says is their responsibility, and the text enters the
architect's prompt like any typed task. The README says so plainly, and `state.json` keeps the
fetched text, so auditability exists after the fact.

This assumes what the person read is what the command fetches. It can diverge — a comment added
after they looked, or a command configured to print more than the description. The divergence
has the same owner; it is not a reason for a gate.

D12. **Empty output is an error, and this is not about trust.** A command that exits 0 and
prints nothing is a broken fetch: auth that failed silently, a wrong ref, an issue with no body.
Without the check, the run would pay an architect to plan from nothing. Deliberately separate
from D11: input sanity, not input judgement.

D13. **This delivery is worth building, and the criterion is ergonomics plus provenance.**
`stargate run "$(gh issue view 42 ...)"` already works; what the feature adds is being friendly,
plus what `$( )` cannot do — recorded provenance, branch naming from the ref, and a named error
when the fetch fails before the architect is paid for.

D14. **Reading a PR as input is included, and the ambiguity dissolves.** A PR carries a diff as
well as a description, but **whoever writes the command decides what "the task" is**: printing
only the description, or description plus diff, is the config author's choice. Not stargate's
problem, and no separate design.

---

# Delivery 2: open the pull request

```bash
stargate run --pr "..."          # or together with --from
```

1. The run proceeds normally to its terminal result.
2. **If the verdict is `APPROVED` and `--pr` was given:** stargate pushes the branch — refusing
   before it tries when there is no remote, when the branch already exists there, or when no
   upstream is configured — and then runs the configured command with `{branch}`, `{title}` and
   the body on stdin.
3. **Any other verdict** (`CHANGES_REQUESTED`, budget exceeded, failing tests): nothing is
   published. It prints where the branch is and the exact command to open the PR by hand.
4. `resume` requires `--pr` again.

## Decisions

D15. **Publishing is never automatic. The boundary is crossed by a person's decision, never by
stargate's.** Not a UX preference: it is what keeps the invariant honest once rewritten.

D16. **The config says *how*; the flag says *whether*.** The `pull_request:` block is a fact
about the environment and authorizes nothing on its own; `--pr` is the decision about that work,
in that invocation. If the config's existence were enough, the decision would have been made
**once, earlier**, and every future run would publish — including one nobody was thinking about.
Standing config must not become standing authorization.

D17. **An unapproved verdict returns the decision.** Whoever typed `--pr` decided before knowing
the verdict. The verdict is information they did not have; handing the choice back is the same
principle, not an exception to it.

D18. **`resume` requires the flag again.** The intent to publish is not written to `state.json`
and does not survive a resume. A persisted "will publish" bit would be exactly the standing
authorization D16 refuses, and `resume` is where it would go unnoticed, because you type only a
run id.

D19. **The push belongs to stargate; the PR belongs to the command.** Stargate owns Git, knows
the branch name, and can **refuse before trying**. Putting the push inside a config `sh -c`
would bring back the `doctor` problem and move the refusal out of code and into a string.

D20. **The PR title comes from the issue when there is one, otherwise the task's first line.**
The architect's `NAME:` is out: it is deliberately short — two to four words, for naming a
branch — and would make a poor title.

D21. **The PR body is where structured findings get a reader.** The first surface where the
findings are read by someone who did not open `summary.md`: verdict, findings table, the test
command and its exit, and the originating issue when there is one.

D22. **Commenting on the tracker is out of scope.** The PR body already delivers the findings to
whoever reads; a comment would add a second remote surface for the same content.

## The invariant

D23. `AGENTS.md` lists among the non-negotiable invariants that the orchestrator "only creates
**local** `stargate/*` branches and worktrees outside the repository", and that it never pushes.
Opening a PR requires a push, so the invariant will be **rewritten, not worked around** — being
worked around is how an invariant dies.

D24. **The wording matters more than it looks.** The rewritten invariant is not "stargate now
publishes" but: **stargate never publishes on its own — it publishes when a person says so, in
that invocation.** The difference between those two sentences is the whole feature.

## Not a shared precondition after all

An earlier draft claimed three features require `settings.commit: true` and should share one
named check. That was wrong, and worth recording so it is not "fixed" later:

- **Fan-out** reads the *setting* (`commit_enabled`, `fanout.py:1070`) and refuses to start.
- **Findings inheritance** reads the *previous run's recorded commit* (`run.py:427`) and
  silently inherits nothing. It never consults the setting.
- **A PR** would need this run to commit.

"This run must commit" and "that run did commit" are different propositions. Only fan-out and
the PR share one, and one of those does not exist yet — an abstraction for two callers, one of
them hypothetical, does not pay for itself.
