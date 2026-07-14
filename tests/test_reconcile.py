import json

from sessionator.reconcile import reconcile
from sessionator.store import Store
from conftest import make_config, make_record, seed_store

CLAUDE_SID = "claude/4f00dc4e-0d36-4bd9-a481-39ef82c19509"
CODEX_SID = "codex/019f5d38-8c93-7f42-8c83-2cc98d365537"

VAULT_GLOB = "**/private-notes/**"
VAULT_PATH = "/Users/ed/Desktop/private-notes/secret.md"


def test_reconcile_ingests_both_harnesses(tmp_path, claude_fixtures, codex_fixtures):
    cfg = make_config(tmp_path, claude_dir=claude_fixtures, codex_dir=codex_fixtures)
    result = reconcile(cfg, kick_backfill=False)

    store = Store(cfg)
    records = store.load()
    assert CLAUDE_SID in records
    assert CODEX_SID in records
    # Sidechain + headless are filtered, not stored.
    assert result.upserted_new == 2
    assert result.by_harness == {"claude": 1, "codex": 1}
    # Every stored record starts pending its summary.
    assert all(r.summary_state == "pending" for r in records.values())


def test_reconcile_writes_excerpt_sidecar_without_private(tmp_path, claude_fixtures):
    cfg = make_config(tmp_path, claude_dir=claude_fixtures)
    reconcile(cfg, kick_backfill=False)
    store = Store(cfg)
    rec = store.load()[CLAUDE_SID]
    assert rec.excerpt_path
    body = store.read_excerpt(rec)
    assert body
    assert "sk-secret-abc123" not in body
    assert "[private]" in body


def test_reconcile_watermark_idempotent(tmp_path, claude_fixtures, codex_fixtures):
    cfg = make_config(tmp_path, claude_dir=claude_fixtures, codex_dir=codex_fixtures)
    reconcile(cfg, kick_backfill=False)
    second = reconcile(cfg, kick_backfill=False)
    # Nothing changed on disk -> no re-extraction.
    assert second.extracted == 0
    assert second.upserted_new == 0


def test_reconcile_exclusion_skips_ingest(tmp_path, claude_fixtures, codex_fixtures):
    cfg = make_config(
        tmp_path, claude_dir=claude_fixtures, codex_dir=codex_fixtures,
        exclusions=["**/proj"],
    )
    result = reconcile(cfg, kick_backfill=False)
    store = Store(cfg)
    assert store.load() == {}
    assert result.excluded == 2


def test_reconcile_retroactive_purge(tmp_path, claude_fixtures, codex_fixtures):
    cfg = make_config(tmp_path, claude_dir=claude_fixtures, codex_dir=codex_fixtures)
    reconcile(cfg, kick_backfill=False)
    store = Store(cfg)
    assert len(store.load()) == 2

    # Add an exclusion and reconcile again: matching records are purged even
    # though the transcripts are unchanged (watermark would otherwise skip them).
    cfg.exclusions = ["**/proj"]
    result = reconcile(cfg, kick_backfill=False)
    assert store.load() == {}
    assert result.purged == 2


def test_reconcile_respects_tombstone(tmp_path, claude_fixtures):
    cfg = make_config(tmp_path, claude_dir=claude_fixtures)
    store = Store(cfg)
    store.write_tombstones([CLAUDE_SID])
    result = reconcile(cfg, kick_backfill=False)
    assert CLAUDE_SID not in store.load()
    assert result.tombstoned_skipped >= 1


def test_reconcile_scrubs_stored_files_on_glob_change(tmp_path):
    # A kept (non-excluded cwd) record carrying a vault path in its files
    # manifest gets that entry redacted when the exclusion set changes.
    cfg = make_config(tmp_path)
    rec = make_record(
        "claude/vault1",
        cwd="/home/ed/proj",
        files=[["C", VAULT_PATH], ["M", "src/a.py"]],
    )
    seed_store(cfg, [rec])

    cfg.exclusions = [VAULT_GLOB]
    result = reconcile(cfg, kick_backfill=False)
    out = Store(cfg).load()["claude/vault1"]
    assert ["scrubbed", "[excluded-path]"] in out.files
    assert ["M", "src/a.py"] in out.files
    assert "private-notes" not in repr(out.files)
    assert result.scrubbed == 1

    # Globs unchanged next run -> no re-scan, files stay clean.
    result2 = reconcile(cfg, kick_backfill=False)
    assert result2.scrubbed == 0
    assert Store(cfg).load()["claude/vault1"].files == out.files


def test_reconcile_scrubs_stored_excerpt_on_glob_change(tmp_path):
    cfg = make_config(tmp_path)
    store = Store(cfg)
    rec = make_record("claude/vault2", cwd="/home/ed/proj")
    rec.excerpt = f"USER: please edit {VAULT_PATH} then run tests"
    rec.excerpt_path = store.write_excerpt(rec)
    store.write({rec.sid: rec})

    cfg.exclusions = [VAULT_GLOB]
    result = reconcile(cfg, kick_backfill=False)
    body = Store(cfg).read_excerpt(Store(cfg).load()["claude/vault2"])
    assert "private-notes" not in body
    assert "[excluded-path]" in body
    assert result.scrubbed == 1


def test_reconcile_scrubs_new_record_at_ingest(tmp_path):
    # An excluded path present in a freshly extracted (kept) session is scrubbed
    # before the record is ever written — both files manifest and excerpt.
    cdir = tmp_path / "claude" / "-work-proj"
    cdir.mkdir(parents=True)
    uuid = "11111111-2222-3333-4444-555555555555"
    lines = [
        {
            "type": "user",
            "cwd": "/work/proj",
            "timestamp": "2026-07-01T10:00:00Z",
            "message": {"role": "user", "content": "do the thing"},
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "opus",
                "content": [
                    {"type": "text", "text": f"writing {VAULT_PATH}"},
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Write",
                        "input": {"file_path": VAULT_PATH},
                    },
                ],
            },
        },
    ]
    (cdir / f"{uuid}.jsonl").write_text("\n".join(json.dumps(x) for x in lines))

    cfg = make_config(
        tmp_path, claude_dir=tmp_path / "claude", exclusions=[VAULT_GLOB]
    )
    result = reconcile(cfg, kick_backfill=False)
    rec = Store(cfg).load()[f"claude/{uuid}"]
    assert result.scrubbed == 1
    assert ["scrubbed", "[excluded-path]"] in rec.files
    assert "private-notes" not in repr(rec.files)
    body = Store(cfg).read_excerpt(rec)
    assert "private-notes" not in body
    assert "[excluded-path]" in body
