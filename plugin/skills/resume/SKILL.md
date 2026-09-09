---
name: resume
description: Resolves a session from the user's local sessionator history and hands back the exact command that re-opens it. This skill should be used when the user invokes /sessionator:resume, or asks to resume a past session, reopen a session by its id or prefix, or pick up where they left off on some earlier piece of work.
---

# /sessionator:resume

Use the invocation arguments, or the user’s request, to find one past session.
Run commands with the host’s shell tool and quote arguments as data.

## Classify the argument first

Treat `$ARGUMENTS` as a session id when it starts with `claude/` or `codex/`, or
when it is a bare hex string of six characters or more. Resolve it directly:

```
sessionator resume $ARGUMENTS
```

Otherwise treat it as search terms:

```
sessionator search $ARGUMENTS --limit 10
```

Rewrite any relative date in the terms into absolute `--since`/`--until` bounds
before running.

Then branch on what came back:

- Exactly one hit: run `sessionator resume` on that hit's sid.
- More than one hit: relay the compact rows verbatim and ask which session to
  resume. Do not guess.
- Zero hits (exit `1`): broaden one rung at a time and say what changed. Drop
  the narrowest filters, then reduce to the single most distinctive term, then
  swap `--repo NAME` for `--cwd NAME`, then run `sessionator status`. Only after
  that, report the session as absent.
- Exit `2`: surface stderr verbatim and stop.

If an id prefix is ambiguous, the CLI lists the candidates. Present them and ask.

## Hand back the line, do not run it

`sessionator resume` prints a single copy-pasteable line, for example
`cd /path/to/repo && claude --resume ID`. Relay it inside a fenced code block
for the user to run themselves. Never execute it: resuming replaces the current
session, and the choice of when to switch belongs to the user.

## Offer the handoff packet

Alongside the line, offer `sessionator show SID` for the Left-off note, open
todos, files manifest, branch, and a transcript tail, which is useful when the
user wants context before switching.

If the shell reports `command not found`, the CLI is not installed. Say so and
offer `uvx --from git+https://github.com/edbrodie/sessionator sessionator status`.
Never install it unprompted.

Full CLI details are in
[CLI reference](../sessionator/references/cli-reference.md).
