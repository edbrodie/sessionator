---
name: find
description: Runs a filtered search over the user's local sessionator history of past Claude Code and Codex sessions. This skill should be used when the user invokes /sessionator:find, or asks to find, list, or filter past sessions by terms, repository, working directory, date range, harness, model, or whether the work was left open.
---

# /sessionator:find

Search the local session index and relay the hits.

## Run it

Use the invocation arguments (or the user’s request when the host does not
provide `$ARGUMENTS`). Quote each shell argument as data:

```
sessionator search $ARGUMENTS
```

With no arguments at all, list the most recent sessions instead:

```
sessionator search --limit 20
```

Before running, rewrite any relative date in `$ARGUMENTS` into absolute
`--since`/`--until` bounds in `YYYY-MM-DD` form, using today's date. A single
day becomes a closed range with the same value on both flags. Leave every other
token exactly as the user typed it.

## Branch on the exit code

- `0`: at least one hit. Relay the compact rows verbatim, newest first, each
  `date · harness(model) · cwd-tail · sid · asked`. Do not reformat them into
  prose. `(summary pending)` in place of the Asked line is normal on a session
  that just ended.
- `1`: zero hits, which is not an error. Broaden one rung at a time and say what
  changed: drop the narrowest filters, then reduce the free-text terms to the
  single most distinctive word, then swap `--repo NAME` for `--cwd NAME` to
  catch worktrees, then run `sessionator status`. Only after that, report the
  session as genuinely absent.
- `2`: an error. Surface stderr verbatim and stop.

If the shell reports `command not found`, the CLI is not installed. Say so and
offer `uvx --from git+https://github.com/edbrodie/sessionator sessionator status`.
Never install it unprompted.

## Follow-ups

Offer `sessionator show SID-PREFIX` for a full record and
`sessionator resume SID-PREFIX` for the re-open line. Relay a resume line for the
user to run; do not execute it.

Flag tables, output shapes, and the exit-code contract live in
[CLI reference](../sessionator/references/cli-reference.md).
