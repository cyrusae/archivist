"""YouTube transcript extraction using youtube-transcript-api."""

import logging
import re
from typing import Optional, List
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound

logger = logging.getLogger("archivist.youtube")

# Match YouTube video IDs from various URL formats
YT_ID_PATTERN = re.compile(
    r'(?:https?://)?(?:www\.)?(?:youtube\.com/(?:watch\?v=|embed/|v/|shorts/)|youtu\.be/)([a-zA-Z0-9_-]{11})'
)

def extract_video_id(url: str) -> Optional[str]:
    """Extract the 11-character video ID from a YouTube URL."""
    match = YT_ID_PATTERN.search(url)
    return match.group(1) if match else None

async def fetch_transcript(url: str) -> Optional[str]:
    """
    Fetch the transcript for a YouTube video and return it as a single string.
    Falls back to None if no transcript is available.
    """
    video_id = extract_video_id(url)
    if not video_id:
        return None

    try:
        # Note: list_transcripts() is blocking, so we'll run it in a thread if needed,
        # but for simplicity in this library we'll use it directly as it's typically fast.
        # Ideally, we would use asyncio.to_thread in a production bot.
        import asyncio
        loop = asyncio.get_running_loop()
        
        def _get_transcript():
            try:
                transcript_list = YouTubeTranscriptApi.get_transcript(video_id)
                return " ".join([t['text'] for t in transcript_list])
            except (TranscriptsDisabled, NoTranscriptFound, Exception) as e:
                logger.warning(f"Could not fetch transcript for {video_id}: {e}")
                return None

        return await loop.run_in_executor(None, _get_transcript)

    except Exception as e:
        logger.error(f"Error in fetch_transcript for {url}: {e}")
        return None
