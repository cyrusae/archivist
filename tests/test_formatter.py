"""Tests for formatter.py — Discord 2000-char limit handling."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from formatter import format_archive_message, _cap, _DISCORD_LIMIT


# ── _cap helper ───────────────────────────────────────────────────────────────

def test_cap_short_string():
    assert _cap("hello", 10) == "hello"


def test_cap_exact_length():
    assert _cap("hello", 5) == "hello"


def test_cap_truncates():
    result = _cap("hello world", 8)
    assert len(result) == 8
    assert result.endswith("…")


def test_cap_empty_string():
    assert _cap("", 10) == ""


# ── format_archive_message ────────────────────────────────────────────────────

def test_basic_message_fits():
    msg = format_archive_message(
        url="https://example.com",
        title="A Short Title",
        gloss="One-liner gloss",
        summary="A short summary.",
        tags=["ai", "python"],
        author_name="alice",
    )
    assert len(msg) <= _DISCORD_LIMIT
    assert "https://example.com" in msg
    assert "A Short Title" in msg


def test_long_title_is_capped():
    title = "X" * 500
    msg = format_archive_message(url="https://example.com", title=title)
    assert len(msg) <= _DISCORD_LIMIT
    # The title in the message should be truncated
    assert "X" * 201 not in msg


def test_long_summary_is_capped():
    summary = "S" * 2000
    msg = format_archive_message(url="https://example.com", summary=summary)
    assert len(msg) <= _DISCORD_LIMIT


def test_long_gloss_is_capped():
    gloss = "G" * 500
    msg = format_archive_message(url="https://example.com", gloss=gloss)
    assert len(msg) <= _DISCORD_LIMIT


def test_long_commentary_is_capped():
    commentary = "C" * 1000
    msg = format_archive_message(url="https://example.com", commentary=commentary)
    assert len(msg) <= _DISCORD_LIMIT


def test_all_fields_long_still_fits():
    """Worst-case: every field is at or over its cap."""
    msg = format_archive_message(
        url="https://example.com/" + "u" * 300,
        title="T" * 400,
        gloss="G" * 300,
        summary="S" * 1000,
        tags=[f"tag{i}" for i in range(20)],
        archive_url="https://archive.is/" + "a" * 100,
        archive_service="archive.is",
        commentary="C" * 800,
        author_name="alice",
        errors=["err1", "err2"],
    )
    assert len(msg) <= _DISCORD_LIMIT


def test_privacy_mode_no_summary():
    msg = format_archive_message(
        url="https://example.com",
        privacy_mode=True,
    )
    assert "Privacy mode" in msg


def test_archive_link_included():
    msg = format_archive_message(
        url="https://example.com",
        archive_url="https://archive.is/abc123",
        archive_service="archive.is",
    )
    assert "archive.is" in msg
    assert "archive.is/abc123" in msg


def test_errors_included():
    msg = format_archive_message(
        url="https://example.com",
        errors=["Fetch failed", "Archive failed"],
    )
    assert "Fetch failed" in msg
    assert "Archive failed" in msg
