"""Format archived link data into Discord messages (and Obsidian-friendly markdown)."""

from datetime import datetime, timezone
from typing import Optional, List

# Discord's hard limit is 2000 chars for normal messages.  Leave a small buffer
# so that any caller-side concatenation (e.g. appending a PDF note) stays safe.
_DISCORD_LIMIT = 1950


def _cap(text: str, n: int) -> str:
    """Truncate *text* to at most *n* characters, appending '…' if cut."""
    if len(text) <= n:
        return text
    return text[: n - 1] + "…"


def format_archive_message(
    url: str,
    title: Optional[str] = None,
    gloss: Optional[str] = None,
    summary: Optional[str] = None,
    tags: Optional[List[str]] = None,
    archive_url: Optional[str] = None,
    archive_service: Optional[str] = None,
    commentary: Optional[str] = None,
    author_name: Optional[str] = None,
    errors: Optional[List[str]] = None,
    privacy_mode: bool = False,
) -> str:
    """
    Build a markdown-formatted Discord message for an archived link.
    
    Designed to be:
    1. Readable in Discord
    2. Copy-pasteable into Obsidian as-is
    """
    # --- Per-field caps (defence-in-depth; ai.py already caps its outputs) ----
    # These prevent a single long field from blowing the 2000-char Discord limit
    # even when the AI returns a correctly-shaped but oversized response.
    if title:
        title = _cap(title, 200)
    if gloss:
        gloss = _cap(gloss, 150)
    if summary:
        summary = _cap(summary, 500)
    if commentary:
        commentary = _cap(commentary, 300)

    lines = []

    # Header: title or URL
    if title:
        lines.append(f"### 📑 {title}")
    else:
        lines.append(f"### 📑 Link archived")

    # Original URL (always shown)
    lines.append(f"🔗 {url}")

    # Archive link
    if archive_url:
        svc_label = archive_service or "archive"
        lines.append(f"🏛️ [{svc_label}]({archive_url})")

    # Gloss (one-line subject)
    if gloss:
        lines.append(f"\n> **{gloss}**")

    # Summary
    if summary:
        lines.append(f"\n{summary}")
    elif privacy_mode:
        lines.append("\n*Privacy mode — summary skipped*")

    # Tags
    if tags:
        tag_str = " ".join(f"`{t}`" for t in tags)
        lines.append(f"\n🏷️ {tag_str}")

    # Commentary (user's note)
    if commentary:
        lines.append(f"\n💬 *{commentary}*")

    # Metadata footer
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    footer_parts = [ts]
    if author_name:
        footer_parts.append(f"saved by {author_name}")
    lines.append(f"\n-# {' · '.join(footer_parts)}")

    # Errors (subtle, at the bottom)
    if errors:
        for err in errors:
            lines.append(f"-# ⚠️ {err}")

    result = "\n".join(lines)

    # Hard backstop — should rarely fire given the per-field caps above, but
    # very long URLs, many error lines, or many tags can still push us over.
    if len(result) > _DISCORD_LIMIT:
        result = result[: _DISCORD_LIMIT - 1] + "…"

    return result
