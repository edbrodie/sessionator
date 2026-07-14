from sessionator.privacy import (
    EXCLUDED_MARKER,
    cwd_excluded,
    path_excluded,
    scrub_files,
    scrub_text,
    strip_private,
)

VAULT_GLOBS = ["**/private-notes/**", "**/private-notes"]

# The two flavours the real store carries: a plain absolute vault path, and the
# dashed ~/.claude/projects encoding where the separator before `private-notes` is
# a `-`, not a `/` (see the T-012 acceptance records).
VAULT_ABS = "/Users/ed/Desktop/private-notes/manual/note.md"
VAULT_DASHED = (
    "/Users/ed/.claude/projects/-Users-ed-Desktop-private-notes/MEMORY.md"
)


def test_strip_private_replaces_span():
    out = strip_private("key is <private>sk-abc-123</private> ok")
    assert "sk-abc-123" not in out
    assert "[private]" in out
    assert out == "key is [private] ok"


def test_strip_private_multiline_and_multiple():
    text = "a <private>one\ntwo</private> b <PRIVATE>three</PRIVATE> c"
    out = strip_private(text)
    assert "one" not in out and "three" not in out
    assert out.count("[private]") == 2


def test_strip_private_passthrough():
    assert strip_private("no tags here") == "no tags here"
    assert strip_private(None) is None
    assert strip_private(123) == 123


def test_cwd_excluded_vault_example():
    glob = "**/private-notes/**"
    assert cwd_excluded("/Users/ed/private-notes/notes", [glob])
    # The vault root itself (no trailing child) also matches.
    assert cwd_excluded("/Users/ed/private-notes", [glob])
    # A sibling must NOT match.
    assert not cwd_excluded("/Users/ed/private-notes-public", [glob])
    assert not cwd_excluded("/Users/ed/projects/other", [glob])


def test_cwd_excluded_empty_inputs():
    assert not cwd_excluded("", ["**/x/**"])
    assert not cwd_excluded("/a/b", [])
    assert not cwd_excluded("/a/b", None)


def test_path_excluded_matches_both_encodings():
    # Plain absolute path AND the dashed project-dir encoding both match, but a
    # sibling (private-notes-public) does not — the relaxation only drops the leading
    # separator, it does not loosen the trailing segment boundary.
    assert path_excluded(VAULT_ABS, VAULT_GLOBS)
    assert path_excluded(VAULT_DASHED, VAULT_GLOBS)
    assert not path_excluded("/Users/ed/private-notes-public/x.md", VAULT_GLOBS)
    assert not path_excluded("/Users/ed/projects/other/x.md", VAULT_GLOBS)
    assert not path_excluded("", VAULT_GLOBS)
    assert not path_excluded(VAULT_ABS, [])


def test_scrub_files_redacts_and_is_idempotent():
    files = [
        ["M", "src/app.py"],
        ["C", VAULT_ABS],
        ["M", VAULT_DASHED],
    ]
    out, n = scrub_files(files, VAULT_GLOBS)
    assert n == 2
    assert out[0] == ["M", "src/app.py"]  # untouched
    assert out[1] == ["scrubbed", EXCLUDED_MARKER]
    assert out[2] == ["scrubbed", EXCLUDED_MARKER]
    # No vault path survives anywhere.
    assert "private-notes" not in repr(out)
    # Second pass is a no-op.
    out2, n2 = scrub_files(out, VAULT_GLOBS)
    assert n2 == 0
    assert out2 == out


def test_scrub_files_empty():
    assert scrub_files([], VAULT_GLOBS) == ([], 0)
    assert scrub_files(None, VAULT_GLOBS) == (None, 0)


def test_scrub_text_redacts_inline_paths():
    text = (
        f"Wrote {VAULT_ABS} and updated {VAULT_DASHED}.\n"
        "Also touched src/keep.py which stays."
    )
    out, n = scrub_text(text, VAULT_GLOBS)
    assert n == 2
    assert "private-notes" not in out
    assert out.count(EXCLUDED_MARKER) == 2
    assert "src/keep.py" in out
    # Idempotent: the marker holds no slash so it is never re-matched.
    out2, n2 = scrub_text(out, VAULT_GLOBS)
    assert n2 == 0
    assert out2 == out


def test_scrub_text_no_globs_or_no_match():
    assert scrub_text("nothing here", VAULT_GLOBS) == ("nothing here", 0)
    assert scrub_text(f"has {VAULT_ABS}", []) == (f"has {VAULT_ABS}", 0)
