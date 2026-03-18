"""Tests for the message parser."""

import sys
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from parser import parse_message


def test_single_url():
    r = parse_message("https://example.com")
    assert r.urls == ["https://example.com"]
    assert r.commentary == ""
    assert r.should_process is True


def test_url_with_commentary():
    r = parse_message("https://example.com cool article about AI")
    assert r.urls == ["https://example.com"]
    assert r.commentary == "cool article about AI"


def test_multiple_urls():
    r = parse_message("https://a.com https://b.com both interesting")
    assert r.urls == ["https://a.com", "https://b.com"]
    assert r.commentary == "both interesting"


def test_nosummary_flag():
    r = parse_message("https://example.com -nosummary")
    assert r.no_summary is True
    assert r.effective_no_summary is True
    assert r.no_tags is False


def test_short_flag():
    r = parse_message("https://example.com -ns")
    assert r.no_summary is True


def test_privacy_flag():
    r = parse_message("https://example.com -privacy")
    assert r.privacy is True
    assert r.effective_no_summary is True
    assert r.effective_no_tags is True
    assert r.no_archive is False


def test_multiple_flags():
    r = parse_message("https://example.com -ns -na interesting stuff")
    assert r.no_summary is True
    assert r.no_archive is True
    assert r.no_tags is False
    assert r.commentary == "interesting stuff"


def test_legacy_optout():
    r = parse_message("https://example.com Archivist, no")
    assert r.opt_out is True
    assert r.should_process is False
    assert r.urls == ["https://example.com"]


def test_legacy_optout_case_insensitive():
    r = parse_message("https://example.com archivist no")
    assert r.opt_out is True


def test_no_urls():
    r = parse_message("just a regular message, no links")
    assert r.urls == []
    assert r.should_process is False


def test_flags_mixed_with_commentary():
    r = parse_message("https://example.com this is -notags a great read -noarchive")
    assert r.no_tags is True
    assert r.no_archive is True
    assert r.commentary == "this is a great read"


def test_complex_url():
    r = parse_message("https://example.com/path?q=test&foo=bar#section")
    assert len(r.urls) == 1
    assert "q=test" in r.urls[0]


if __name__ == "__main__":
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    passed = 0
    for test in tests:
        try:
            test()
            print(f"  ✓ {test.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  ✗ {test.__name__}: {e}")
    print(f"\n{passed}/{len(tests)} tests passed")
    if passed < len(tests):
        sys.exit(1)
