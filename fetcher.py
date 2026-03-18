"""Fetch web pages and extract readable content."""

import logging
from dataclasses import dataclass
from typing import Optional

import aiohttp
from readability import Document  # readability-lxml
from strip_tags import strip_tags

logger = logging.getLogger("archivist.fetcher")

# Reasonable limits
MAX_CONTENT_LENGTH = 10 * 1024 * 1024  # 10MB
FETCH_TIMEOUT = 20  # seconds
# Truncate extracted text sent to LLMs
MAX_TEXT_FOR_LLM = 50000  # characters (Gemini can handle more, but let's be reasonable)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; Archivist/1.0; +https://github.com/watcher/archivist)"
}


@dataclass
class FetchedPage:
    url: str
    title: Optional[str] = None
    text: Optional[str] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.text is not None and self.error is None


async def fetch_page(url: str) -> FetchedPage:
    """
    Fetch a URL and extract readable text content.
    Uses readability-lxml for extraction and strip-tags for cleaning.
    """
    try:
        timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout, headers=HEADERS) as session:
            async with session.get(url, allow_redirects=True) as resp:
                if resp.status != 200:
                    return FetchedPage(url=url, error=f"HTTP {resp.status}")

                content_type = resp.headers.get("Content-Type", "")
                if "text/html" not in content_type and "text/plain" not in content_type:
                    return FetchedPage(
                        url=url,
                        error=f"Non-HTML content: {content_type.split(';')[0]}",
                    )

                # Check content length
                cl = resp.headers.get("Content-Length")
                if cl and int(cl) > MAX_CONTENT_LENGTH:
                    return FetchedPage(url=url, error="Content too large")

                html = await resp.text(errors="replace")

        # Extract readable content
        doc = Document(html)
        title = doc.short_title() or doc.title()
        
        # doc.summary() returns the main content as HTML
        summary_html = doc.summary()

        # Use strip-tags to get clean text from the summary HTML
        # We'll use minify=True and remove_blank_lines=True to reduce token usage
        text = strip_tags(
            summary_html,
            minify=True,
            remove_blank_lines=True
        )

        if not text or len(text) < 50:
            # If readability failed to find a "main" content, try stripping the whole body
            text = strip_tags(html, minify=True, remove_blank_lines=True)
            
            if not text or len(text) < 50:
                return FetchedPage(url=url, title=title, error="Could not extract meaningful content")

        # Truncate for LLM consumption
        if len(text) > MAX_TEXT_FOR_LLM:
            text = text[:MAX_TEXT_FOR_LLM] + "... [truncated]"

        return FetchedPage(url=url, title=title, text=text)

    except aiohttp.ClientError as e:
        return FetchedPage(url=url, error=f"Fetch error: {type(e).__name__}")
    except Exception as e:
        logger.exception(f"Unexpected error fetching {url}")
        return FetchedPage(url=url, error=f"Error: {e}")
