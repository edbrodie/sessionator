import tomllib

from sessionator import config


def test_first_run_autodetect_and_write(tmp_path, monkeypatch):
    cfg_home = tmp_path / "cfg"
    data_home = tmp_path / "data"
    claude_root = tmp_path / "fakeclaude"
    codex_root = tmp_path / "fakecodex"
    (claude_root / "projects").mkdir(parents=True)
    (codex_root / "sessions").mkdir(parents=True)

    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_root))
    monkeypatch.setenv("CODEX_HOME", str(codex_root))

    cfg = config.load()
    assert cfg.first_run is True
    assert cfg.path.exists()
    # Detected both transcript dirs.
    assert cfg.sources["claude"].enabled is True
    assert cfg.sources["claude"].transcript_dir == str(claude_root / "projects")
    assert cfg.sources["codex"].enabled is True
    assert cfg.data_dir == data_home / "sessionator"

    # Written file is valid TOML the loader can re-read without re-detecting.
    with open(cfg.path, "rb") as f:
        raw = tomllib.load(f)
    assert raw["schema_version"] == config.CONFIG_SCHEMA_VERSION
    assert raw["summarize"]["claude"]["model"] == "opus-4.8"
    assert raw["summarize"]["codex"]["model"] == "gpt-5.6-luna"

    cfg2 = config.load()
    assert cfg2.first_run is False
    assert cfg2.sources["claude"].transcript_dir == str(claude_root / "projects")


def test_disabled_source_when_dir_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nope-claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "nope-codex"))
    cfg = config.load()
    assert cfg.sources["claude"].enabled is False
    assert cfg.sources["codex"].enabled is False


def test_exclusions_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "c"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "x"))
    cfg = config.load()
    cfg.exclusions = ["**/private-notes/**"]
    config.save(cfg)
    cfg2 = config.load()
    assert cfg2.exclusions == ["**/private-notes/**"]


def test_summarizer_cli_fallback(tmp_path):
    from sessionator.config import Config, Source, DEFAULT_SUMMARIZE
    cfg = Config(
        data_dir=tmp_path,
        exclusions=[],
        sources={
            "claude": Source("claude", True, "/x", "/usr/bin/claude"),
            "codex": Source("codex", True, "/y", None),
        },
        summarize={k: dict(v) for k, v in DEFAULT_SUMMARIZE.items()},
        path=tmp_path / "c.toml",
    )
    # Same-harness available.
    assert cfg.summarizer_cli("claude") == ("claude", "/usr/bin/claude")
    # Codex has no CLI -> falls back to claude.
    assert cfg.summarizer_cli("codex") == ("claude", "/usr/bin/claude")
