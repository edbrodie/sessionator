"""Configuration: XDG TOML, auto-written on first run (T-005 resolution).

First run auto-detects the ``claude``/``codex`` CLIs and their transcript dirs,
writes ``$XDG_CONFIG_HOME/sessionator/config.toml`` (``~/.config`` fallback) with
detected values plus shipped summarizer defaults, and prints what it found — no
wizard, zero questions. Config is read with the stdlib ``tomllib``; it is
written by a tiny hand-rolled emitter (the file is a trivial, known subset).

Data (store, sidecars, watermarks, lockfiles) lives under
``$XDG_DATA_HOME/sessionator`` (``~/.local/share`` fallback).
"""

from __future__ import annotations

import os
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_SCHEMA_VERSION = 2

# Shipped summarizer defaults (T-005): per-harness model/effort. Summaries are
# small, frequent, and per-segment, so the default is the cheapest capable model.
DEFAULT_SUMMARIZE = {
    "claude": {"model": "haiku", "effort": "medium"},
    "codex": {"model": "gpt-5.6-luna", "reasoning": "high"},
}

# Which CLI summarizes, regardless of the session's own harness: "claude" |
# "codex" | "same" (the pre-v2 behaviour of preferring the session's harness).
# Default "claude" so a cheap Haiku call summarizes Codex sessions too.
PREFER_VALUES = ("claude", "codex", "same")
DEFAULT_PREFER = "claude"

# v1 shipped this Claude model default; a config still carrying it is using the
# shipped value, not a user choice, so the v2 migration may replace it.
_V1_CLAUDE_MODEL = "opus-4.8"

# Code default is empty: a public tool ships no machine-specific exclusions. A
# user (or an operator setting up a machine with a local-only vault) adds globs
# to the written config — see docs/index-design.md and the T-003 worked example
# ``**/private-notes/**``.
DEFAULT_EXCLUSIONS: list[str] = []


def _xdg_config_home() -> Path:
    v = os.environ.get("XDG_CONFIG_HOME")
    return Path(v) if v else Path.home() / ".config"


def _xdg_data_home() -> Path:
    v = os.environ.get("XDG_DATA_HOME")
    return Path(v) if v else Path.home() / ".local" / "share"


def config_path() -> Path:
    return _xdg_config_home() / "sessionator" / "config.toml"


def default_data_dir() -> Path:
    return _xdg_data_home() / "sessionator"


def _detect_claude_dir() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    root = Path(base) if base else Path.home() / ".claude"
    return root / "projects"


def _detect_codex_dir() -> Path:
    base = os.environ.get("CODEX_HOME")
    root = Path(base) if base else Path.home() / ".codex"
    return root / "sessions"


@dataclass
class Source:
    name: str
    enabled: bool
    transcript_dir: str
    cli: str | None  # resolved CLI path, or None if not on PATH


@dataclass
class Config:
    data_dir: Path
    exclusions: list[str]
    sources: dict[str, Source]
    summarize: dict[str, dict]
    path: Path
    summarize_prefer: str = DEFAULT_PREFER
    first_run: bool = False
    detection_notes: list[str] = field(default_factory=list)

    # --- Derived data-dir locations -------------------------------------
    @property
    def store_path(self) -> Path:
        return self.data_dir / "store.jsonl"

    @property
    def index_path(self) -> Path:
        return self.data_dir / "index.db"

    @property
    def transcripts_dir(self) -> Path:
        return self.data_dir / "transcripts"

    @property
    def watermarks_path(self) -> Path:
        return self.data_dir / "watermarks.json"

    @property
    def tombstones_path(self) -> Path:
        return self.data_dir / "tombstones.json"

    @property
    def store_lock_path(self) -> Path:
        return self.data_dir / "store.lock"

    @property
    def backfill_lock_path(self) -> Path:
        return self.data_dir / "backfill.lock"

    @property
    def backfill_log_path(self) -> Path:
        return self.data_dir / "backfill.log"

    def summarizer_cli(self, harness: str) -> tuple[str, str] | None:
        """The CLI to summarize a session of ``harness``: try the configured
        ``[summarize].prefer`` CLI first (``same`` = the session's own harness),
        then the rest. Returns ``(harness_of_cli, cli_path)`` or None if no CLI
        is installed."""
        prefer = self.summarize_prefer
        if prefer not in PREFER_VALUES:
            prefer = DEFAULT_PREFER
        wanted = [harness] if prefer == "same" else [prefer, harness]
        order = []
        for h in wanted + ["claude", "codex"]:
            if h not in order:
                order.append(h)
        for h in order:
            src = self.sources.get(h)
            if src and src.cli:
                return (h, src.cli)
        return None


def _detect() -> tuple[dict[str, Source], list[str]]:
    notes = []
    sources = {}
    for name, dirfn in (("claude", _detect_claude_dir), ("codex", _detect_codex_dir)):
        tdir = dirfn()
        cli = shutil.which(name)
        enabled = tdir.is_dir()
        sources[name] = Source(
            name=name, enabled=enabled, transcript_dir=str(tdir), cli=cli
        )
        notes.append(
            f"{name}: transcripts {'found' if enabled else 'not found'} at {tdir}; "
            f"CLI {'at ' + cli if cli else 'not on PATH'}"
        )
    return sources, notes


def _toml_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _toml_str_array(items) -> str:
    inner = ", ".join(f'"{_toml_escape(str(i))}"' for i in items)
    return f"[{inner}]"


def _render_toml(cfg: Config) -> str:
    lines = [
        "# sessionator configuration (auto-written on first run).",
        "# Regenerate by deleting this file and running any command again.",
        f"schema_version = {CONFIG_SCHEMA_VERSION}",
        "",
        "[data]",
        f'dir = "{_toml_escape(str(cfg.data_dir))}"',
        "",
        "[exclusions]",
        "# Session cwd globs to exclude from ingest (retroactively purged on change).",
        '# Example: cwd_globs = ["**/private-notes/**"]',
        f"cwd_globs = {_toml_str_array(cfg.exclusions)}",
        "",
    ]
    for name in ("claude", "codex"):
        src = cfg.sources[name]
        lines += [
            f"[sources.{name}]",
            f"enabled = {'true' if src.enabled else 'false'}",
            f'transcript_dir = "{_toml_escape(src.transcript_dir)}"',
        ]
        if src.cli:
            lines.append(f'cli = "{_toml_escape(src.cli)}"')
        else:
            lines.append("# cli = \"\"  # not detected on PATH")
        lines.append("")
    lines += [
        "[summarize]",
        "# Which CLI summarizes: claude | codex | same (the session's own harness).",
        f'prefer = "{_toml_escape(cfg.summarize_prefer)}"',
        "",
    ]
    for name in ("claude", "codex"):
        s = cfg.summarize.get(name, DEFAULT_SUMMARIZE[name])
        lines.append(f"[summarize.{name}]")
        for k, v in s.items():
            lines.append(f'{k} = "{_toml_escape(str(v))}"')
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _write_config(cfg: Config) -> None:
    cfg.path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg.path.with_suffix(".toml.tmp")
    tmp.write_text(_render_toml(cfg), encoding="utf-8")
    os.replace(tmp, cfg.path)


def _parse(raw: dict, path: Path) -> Config:
    data = raw.get("data") or {}
    data_dir = Path(data.get("dir") or str(default_data_dir()))
    exclusions = list((raw.get("exclusions") or {}).get("cwd_globs") or [])
    sources = {}
    raw_sources = raw.get("sources") or {}
    for name in ("claude", "codex"):
        s = raw_sources.get(name) or {}
        sources[name] = Source(
            name=name,
            enabled=bool(s.get("enabled", False)),
            transcript_dir=str(s.get("transcript_dir") or ""),
            cli=(s.get("cli") or None),
        )
    summarize = {}
    raw_sum = raw.get("summarize") or {}
    for name in ("claude", "codex"):
        summarize[name] = dict(DEFAULT_SUMMARIZE[name])
        summarize[name].update(raw_sum.get(name) or {})
    prefer = raw_sum.get("prefer")
    if prefer not in PREFER_VALUES:
        prefer = DEFAULT_PREFER
    return Config(
        data_dir=data_dir,
        exclusions=exclusions,
        sources=sources,
        summarize=summarize,
        path=path,
        summarize_prefer=prefer,
    )


def _migrate(cfg: Config, raw: dict) -> bool:
    """Upgrade an older on-disk config in memory; True when it must be rewritten.

    Only the *shipped* v1 model default is replaced — a model the user chose is
    never overwritten. The rewrite also stamps the new schema_version so the
    migration runs once.
    """
    if int(raw.get("schema_version") or 1) >= CONFIG_SCHEMA_VERSION:
        return False
    if (cfg.summarize.get("claude") or {}).get("model") == _V1_CLAUDE_MODEL:
        cfg.summarize["claude"]["model"] = DEFAULT_SUMMARIZE["claude"]["model"]
    return True


def load(*, auto_write: bool = True) -> Config:
    """Load config, auto-writing it on first run. When the file is absent and
    ``auto_write`` is True, detection runs, the file is written, and the returned
    Config has ``first_run=True`` with ``detection_notes`` populated."""
    path = config_path()
    if path.exists():
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        cfg = _parse(raw, path)
        if _migrate(cfg, raw):
            try:
                _write_config(cfg)
            except OSError:  # read-only config dir: run with the migrated values
                pass
        return cfg

    # First run: detect, build, (optionally) write.
    sources, notes = _detect()
    cfg = Config(
        data_dir=default_data_dir(),
        exclusions=list(DEFAULT_EXCLUSIONS),
        sources=sources,
        summarize={k: dict(v) for k, v in DEFAULT_SUMMARIZE.items()},
        path=path,
        first_run=True,
        detection_notes=notes,
    )
    if auto_write:
        _write_config(cfg)
    return cfg


def save(cfg: Config) -> None:
    """Persist a Config back to disk (used when exclusions are edited)."""
    _write_config(cfg)
