"""Tests for youtube.py — URL pattern and transcript helpers."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from youtube import extract_video_id


def test_standard_watch_url():
    assert extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_short_url():
    assert extract_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_mobile_url():
    assert extract_video_id("https://m.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_music_url():
    assert extract_video_id("https://music.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_shorts_url():
    assert extract_video_id("https://www.youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_embed_url():
    assert extract_video_id("https://www.youtube.com/embed/dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_no_video_id():
    assert extract_video_id("https://example.com/article") is None


def test_non_youtube_url():
    assert extract_video_id("https://vimeo.com/123456789") is None


def test_url_with_extra_params():
    result = extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s&list=PL1234")
    assert result == "dQw4w9WgXcQ"
