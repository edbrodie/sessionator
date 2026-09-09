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
    assert raw["summarize"]["claude"]["model"] == "haiku"
    assert raw["summarize"]["prefer"] == "claude"
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


# --- v2: haiku default, prefer, migration -----------------------------------


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "c"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "x"))
    return config.config_path()


def test_default_claude_model_is_haiku():
    assert config.DEFAULT_SUMMARIZE["claude"]["model"] == "haiku"
    assert config.CONFIG_SCHEMA_VERSION == 2


V1_CONFIG = """schema_version = 1

[data]
dir = "{data}"

[exclusions]
cwd_globs = []

[sources.claude]
enabled = true
transcript_dir = "{data}/p"
cli = "/usr/bin/claude"

[sources.codex]
enabled = false
transcript_dir = ""

[summarize.claude]
model = "{model}"
effort = "medium"

[summarize.codex]
model = "gpt-5.6-luna"
reasoning = "high"
"""


def _write_v1(tmp_path, monkeypatch, model):
    path = _isolate(tmp_path, monkeypatch)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(V1_CONFIG.format(data=str(tmp_path), model=model))
    return path


def test_v1_migration_rewrites_shipped_model_and_persists(tmp_path, monkeypatch):
    path = _write_v1(tmp_path, monkeypatch, "opus-4.8")
    cfg = config.load()
    assert cfg.summarize["claude"]["model"] == "haiku"
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    assert raw["schema_version"] == 2
    assert raw["summarize"]["claude"]["model"] == "haiku"
    # Idempotent: a second load leaves it alone.
    assert config.load().summarize["claude"]["model"] == "haiku"


def test_v1_migration_preserves_custom_model(tmp_path, monkeypatch):
    _write_v1(tmp_path, monkeypatch, "my-own-model")
    cfg = config.load()
    assert cfg.summarize["claude"]["model"] == "my-own-model"
    assert cfg.summarize_prefer == "claude"


def test_prefer_ordering():
    from sessionator.config import Config, DEFAULT_SUMMARIZE, Source

    def mk(prefer):
        return Config(
            data_dir=".",
            exclusions=[],
            sources={
                "claude": Source("claude", True, "/x", "/bin/claude"),
                "codex": Source("codex", True, "/y", "/bin/codex"),
            },
            summarize={k: dict(v) for k, v in DEFAULT_SUMMARIZE.items()},
            path=".",
            summarize_prefer=prefer,
        )

    # Default prefers claude for both harnesses (cheap Haiku everywhere).
    assert mk("claude").summarizer_cli("codex") == ("claude", "/bin/claude")
    assert mk("claude").summarizer_cli("claude") == ("claude", "/bin/claude")
    # "same" is the pre-v2 behaviour.
    assert mk("same").summarizer_cli("codex") == ("codex", "/bin/codex")
    assert mk("codex").summarizer_cli("claude") == ("codex", "/bin/codex")
    # Bogus value falls back to the default, never crashes.
    assert mk("nonsense").summarizer_cli("codex") == ("claude", "/bin/claude")


def test_prefer_roundtrips_through_toml(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    cfg = config.load()
    cfg.summarize_prefer = "same"
    config.save(cfg)
    assert config.load().summarize_prefer == "same"
