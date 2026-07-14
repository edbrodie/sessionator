# sessionator — Claude Code plugin

The Claude Code wrapper around the [`sessionator`](../README.md) CLI: a `/sessionator`
skill for searching and resuming your past sessions from inside a conversation,
plus a `SessionEnd` hook that keeps the local history current automatically.

This plugin is a thin wrapper. All the work — indexing, search, summarization —
lives in the `sessionator` CLI and stays on your machine. Install the CLI first
(see the [main README](../README.md)); the plugin shells out to it.

## Install

The plugin is distributed through a Claude Code marketplace. Add the marketplace,
then enable the plugin:

```
/plugin marketplace add edbrodie/sessionator
/plugin install sessionator
```

Or point the marketplace at a local checkout of this repository:

```
/plugin marketplace add /path/to/sessionator
/plugin install sessionator
```

Verify the CLI is reachable from the skill with `sessionator status`. If the CLI
is not installed, the skill will say so and the hook is a silent no-op.

## What the hook does

At the end of every Claude Code session, the plugin runs
`hooks/session-end-ingest.sh`. That script:

1. Reads and discards the `SessionEnd` payload so session teardown never blocks.
2. Checks whether the `sessionator` CLI is on `PATH`. If it is not, it exits 0
   and does nothing.
3. If it is, kicks a **detached** `sessionator ingest` in the background and
   returns immediately. Session shutdown never waits on it.

`ingest` scans your local transcript directories, writes a deterministic record
for the finished session, and spawns a short-lived, self-terminating backfill
that generates the five-field summaries. No daemon runs, and nothing is scheduled.

### What runs at session end, and where the data goes

- **Runs:** the local `sessionator` CLI only, in the background, best-effort.
- **Reads:** your own Claude Code transcript files.
- **Writes:** the local sessionator store and index under
  `$XDG_DATA_HOME/sessionator` (`~/.local/share/sessionator` by default).
- **Sends:** nothing off the machine. The CLI makes zero network calls; summary
  generation uses whichever agent CLI you already have installed, locally.

The hook is Claude-side only. It installs no Codex-side hooks and never reads or
writes `~/.codex` configuration.

For the full local-only privacy pledge — no telemetry, no phone-home, reads only
your own transcripts, CI-enforced — see the
[main repository README](../README.md).

## Uninstall

```
/plugin uninstall sessionator
```

Removing the plugin removes the skill and the hook. Your local session store is
left untouched; delete `$XDG_DATA_HOME/sessionator` to remove it, or use
`sessionator forget` for selective retirement.
