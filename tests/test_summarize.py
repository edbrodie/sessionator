from sessionator import summarize


def test_build_prompt_has_markers():
    chunk = [("claude/a", "USER: hi\n\nASSISTANT: done"), ("codex/b", "USER: yo")]
    prompt = summarize.build_prompt(chunk)
    assert "@@S1@@" in prompt
    assert "@@S2@@" in prompt
    assert "**Next steps:**" in prompt


def test_split_and_parse_roundtrip():
    raw = (
        "@@S1@@\n"
        "- **Asked:** fix the auth bug\n"
        "- **Learned:** the token was double-encoded\n"
        "- **Completed:** patched the handler and pushed\n"
        "- **Left off:** green build on main\n"
        "- **Next steps:** None\n"
        "- **Resolved:** done\n"
        "@@S2@@\n"
        "- **Asked:** refactor parser\n"
        "- **Learned:** \n"
        "- **Completed:** split the tokenizer\n"
        "- **Left off:** tests still red\n"
        "- **Next steps:** fix the failing case\n"
        "- **Resolved:** open\n"
    )
    per = summarize._split_by_marker(raw)
    assert set(per) == {"S1", "S2"}

    f1 = summarize._parse_fields(per["S1"])
    assert f1["summary"]["asked"] == "fix the auth bug"
    assert f1["summary"]["learned"] == "the token was double-encoded"
    assert f1["summary"]["next_steps"] == "None"
    assert f1["resolved"] == "done"

    f2 = summarize._parse_fields(per["S2"])
    assert f2["summary"]["learned"] == ""  # blank field tolerated
    assert f2["resolved"] == "open"


def test_parse_fields_rejects_bad_resolved():
    body = "- **Asked:** something\n- **Resolved:** maybe\n"
    fields = summarize._parse_fields(body)
    assert "resolved" not in fields  # invalid enum dropped
    assert fields["summary"]["asked"] == "something"


def test_parse_fields_none_when_empty():
    assert summarize._parse_fields("no labels at all") is None


def test_cap_excerpt():
    text = "x" * 20000
    capped = summarize._cap_excerpt(text)
    assert len(capped) < len(text)
    assert "[trimmed]" in capped


def test_prompt_carries_sentinel_and_is_detected_as_pollution():
    from sessionator.adapters._common import SUMMARIZER_SENTINEL, Walker

    prompt = summarize.build_prompt([("claude/a", "USER: hi")])
    assert prompt.startswith(SUMMARIZER_SENTINEL)

    # A transcript whose first human turn is our own prompt is self-pollution.
    w = Walker()
    w.add_user(prompt)
    assert w.is_summarizer_pollution() is True
    assert w.finish(fallback_day="2026-01-01") is None

    # A real session is not.
    w2 = Walker()
    w2.add_user("fix the flaky auth test")
    assert w2.is_summarizer_pollution() is False
