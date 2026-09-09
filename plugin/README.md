# sessionator: Claude Code plugin

The Claude Code wrapper around the
[`sessionator`](https://github.com/edbrodie/sessionator) CLI: three skills for
searching and resuming your past sessions from inside a conversation, plus
capture hooks that keep the local history current automatically.

This plugin is a thin wrapper. All the work (indexing, search, summarization)
lives in the `sessionator` CLI and stays on your machine. The plugin shells out
to it.

## Prerequisites

Install the CLI first:

```
uv tool install git+https://github.com/edbrodie/sessionator
```

Or run it without installing:

```
uvx --from git+https://github.com/edbrodie/sessionator sessionator status
```

This plugin requires CLI version **0.2.0 or newer**. The hooks call
`sessionator ingest --hook`, which older versions do not have. Check with
`sessionator status`.

## Install

The plugin ships from a marketplace in the same repository:

```
/plugin marketplace add edbrodie/sessionator
/plugin install sessionator@sessionator
```

Or point the marketplace at a local checkout:

```
/plugin marketplace add /path/to/sessionator
/plugin install sessionator@sessionator
```

## Local development

```
claude --plugin-dir ./plugin
```

`/reload-plugins` picks up skill and README edits in the running session. Hook
changes do not hot-swap: exit and restart Claude Code, then confirm with
`/hooks`.

## What you get

| Skill | Invocation | Purpose |
|---|---|---|
| `sessionator` | Automatic | Fires on questions like "when did I work on X", "did I already do X", "resume that session". Searches the index and hands back a resume line. |
| `find` | `/sessionator:find` | Explicit filtered search: terms plus `--repo`, `--cwd`, `--since`, `--until`, `--harness`, `--model`, `--resolved`, `--format`. |
| `resume` | `/sessionator:resume` | Resolves a session id prefix or search terms to the exact command that re-opens the session. |

The skills relay a resume line for you to run. They never run it themselves,
because resuming replaces the current session.

## What the hooks do

Two hooks, both pointing at `hooks/scripts/ingest-hook.sh`:

- **PreCompact** (asynchronous, 600 s budget): before context is compacted, cut
  an incremental summary segment so the pre-compaction work is not lost.
- **SessionEnd** (10 s budget): cut the final segment when the session finishes.

The script itself does almost nothing. It prepends `~/.local/bin` to `PATH`,
checks the per-project opt-out, and pipes the hook payload into
`sessionator ingest --hook`. The CLI spools the payload atomically, detaches a
worker, and returns in well under a second. Neither hook can block compaction or
session teardown, and both exit 0 unconditionally.

The detached worker ingests only that one transcript, writes a deterministic
record, cuts a summary segment at the current position, and folds it into the
running five-field summary with a single Haiku call through the local `claude`
CLI, over a capped, privacy-stripped excerpt. No daemon runs and nothing is
scheduled.

### Where the data goes

- **Runs:** the local `sessionator` CLI, in the background, best-effort.
- **Reads:** your own Claude Code transcript files.
- **Writes:** the local sessionator store and index under
  `$XDG_DATA_HOME/sessionator` (`~/.local/share/sessionator` by default).
- **Sends:** nothing off the machine beyond the one Haiku summary call your
  local `claude` CLI makes on your own account.

### Codex

This plugin installs nothing Codex-side. It writes no Codex hooks and does not
read or write `~/.codex` configuration.

The CLI reads `~/.codex/sessions` and `~/.codex/archived_sessions` when it
ingests, exactly as it always has, so Codex sessions appear in search without
any hook at all. Live Codex capture (compaction-time and session-end segments)
is opt-in and separate: run `sessionator setup codex`, then trust the entries
with Codex's own `/hooks` command. `sessionator setup codex --remove` unmerges
them and leaves `config.toml` untouched.

For the full local-only privacy pledge (no telemetry, no phone-home, reads only
your own transcripts, CI-enforced) see the
[main repository README](https://github.com/edbrodie/sessionator#privacy).

## Opting out per project

Create `.claude/sessionator.local.md` in a project you do not want captured:

```markdown
---
enabled: false
---

Sessions in this project are not recorded by sessionator.
```

The hooks read that frontmatter and exit immediately. An absent file means
capture is enabled. Add `.claude/*.local.md` to your `.gitignore`.

## Uninstall

```
/plugin uninstall sessionator@sessionator
```

That removes the skills and the hooks. Your local session store is left
untouched: delete `$XDG_DATA_HOME/sessionator` to remove it, or use
`sessionator forget` for selective retirement. If you ran `sessionator setup
codex`, run `sessionator setup codex --remove` as well.
