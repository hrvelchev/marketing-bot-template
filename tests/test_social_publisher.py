"""X thread splitter + posting logic, and the IG prompt formatter."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.social_publisher import OPENAI_PROMPTS, XPublisher, format_ig_prompt

split = XPublisher._split_into_thread


# ---- _split_into_thread --------------------------------------------------

def test_short_text_is_single_part():
    assert split("short tweet") == ["short tweet"]


def test_exactly_280_chars_is_single_part():
    text = "x" * 280
    assert split(text) == [text]


def test_split_prefers_sentence_boundary():
    first = "A" * 200 + " end of part one"
    second = "Start of part two " + "B" * 200
    parts = split(first + ". " + second)
    assert parts == [first, second]
    assert all(len(p) <= 280 for p in parts)


def test_split_prefers_paragraph_boundary():
    first = "First paragraph " + "A" * 150
    second = "Second paragraph " + "B" * 150
    parts = split(first + "\n\n" + second)
    assert parts == [first, second]


def test_no_separator_falls_back_to_hard_cut():
    text = "x" * 600
    parts = split(text)
    assert parts == ["x" * 280, "x" * 280, "x" * 40]


def test_early_separator_is_ignored():
    # A boundary in the first half (< max_len // 2) must not be used —
    # it would create a tiny fragment. The hard cut applies instead.
    text = "Short intro. " + "y" * 500
    parts = split(text)
    assert len(parts[0]) == 280
    assert all(len(p) <= 280 for p in parts)


def test_split_preserves_all_words():
    words = [f"word{i}" for i in range(120)]
    text = " ".join(words)
    parts = split(text)
    assert " ".join(parts).split() == words
    assert all(len(p) <= 280 for p in parts)


# ---- XPublisher.post -----------------------------------------------------

def make_publisher(monkeypatch):
    pub = XPublisher(
        api_key="test-key-not-real",
        api_secret="test-secret-not-real",
        access_token="test-token-not-real",
        access_token_secret="test-token-secret-not-real",
        handle="TestHandle",
    )
    calls = []

    class FakeTweepyClient:
        def create_tweet(self, *, text, in_reply_to_tweet_id=None):
            calls.append({"text": text, "reply_to": in_reply_to_tweet_id})
            return SimpleNamespace(data={"id": 111111000 + len(calls)})

    monkeypatch.setattr(pub, "_client", lambda: FakeTweepyClient())
    return pub, calls


def test_post_single_tweet_with_hashtags_merged(monkeypatch):
    pub, calls = make_publisher(monkeypatch)
    url = pub.post("Launch day.", hashtags="#test #fake")
    assert url == "https://x.com/TestHandle/status/111111001"
    assert len(calls) == 1
    assert calls[0]["text"] == "Launch day.\n\n#test #fake"
    assert calls[0]["reply_to"] is None


def test_post_threads_long_text_with_reply_chain(monkeypatch):
    pub, calls = make_publisher(monkeypatch)
    text = ("Sentence one is here. " * 20).strip()  # ~440 chars -> 2 parts
    url = pub.post(text)
    assert len(calls) == 2
    assert calls[0]["reply_to"] is None
    assert calls[1]["reply_to"] == "111111001"
    assert url == "https://x.com/TestHandle/status/111111001"


def test_post_overflowing_hashtags_get_own_tweet(monkeypatch):
    pub, calls = make_publisher(monkeypatch)
    text = "z" * 275  # fits in one tweet; no room left for hashtags
    pub.post(text, hashtags="#test #fake #placeholder")
    assert len(calls) == 2
    assert calls[0]["text"] == text
    assert calls[1]["text"] == "#test #fake #placeholder"
    assert calls[1]["reply_to"] == "111111001"


def test_post_unconfigured_returns_none():
    pub = XPublisher(api_key="", api_secret="",
                     access_token="", access_token_secret="")
    assert pub.post("hello") is None


def test_post_swallows_client_errors(monkeypatch):
    pub, _ = make_publisher(monkeypatch)

    def boom():
        raise RuntimeError("api down")

    monkeypatch.setattr(pub, "_client", boom)
    assert pub.post("hello") is None


# ---- format_ig_prompt ----------------------------------------------------

def test_format_ig_prompt_substitutes_fields():
    out = format_ig_prompt("daily_recap", {
        "date": "2026-01-15", "stat_1": "+1.23%", "stat_2": "4 events",
        "highlight": "+0.80%", "bullet_1": "b1", "bullet_2": "b2",
    })
    assert "Date: 2026-01-15" in out
    assert "KEY_STAT_1: +1.23%" in out


def test_format_ig_prompt_unknown_type_raises():
    with pytest.raises(ValueError, match="unknown post_type"):
        format_ig_prompt("quarterly_recap", {})


def test_format_ig_prompt_missing_field_raises_keyerror():
    with pytest.raises(KeyError):
        format_ig_prompt("daily_recap", {"date": "2026-01-15"})


def test_all_prompt_templates_have_accuracy_rules():
    for name, template in OPENAI_PROMPTS.items():
        assert "CRITICAL TEXT-RENDERING RULES" in template, name
