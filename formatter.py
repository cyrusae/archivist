"""Format archived link data into Discord messages (and Obsidian-friendly markdown)."""

from datetime import datetime, timezone
from typing import Optional, List


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

    return "\n".join(lines)
