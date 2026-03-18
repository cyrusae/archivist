"""Parse Discord messages to extract URLs, flags, and commentary."""

import re
from dataclasses import dataclass, field
from typing import List


# Match URLs (http/https)
URL_PATTERN = re.compile(
    r'https?://[^\s<>\[\]()"\'{}\|\\^`]+'
)

# Exclude internal Discord links and common cruft
EXCLUDE_PATTERNS = [
    re.compile(r'discord\.com/channels/\d+/\d+/\d+', re.IGNORECASE), # Message links
    re.compile(r'discord\.gg/\w+', re.IGNORECASE),                    # Invites
    re.compile(r'discordapp\.com/invites/\w+', re.IGNORECASE),
]

# ... rest of file
FLAGS = {
    "-nosummary": "no_summary",
    "-ns": "no_summary",
    "-notags": "no_tags",
    "-nt": "no_tags",
    "-noarchive": "no_archive",
    "-na": "no_archive",
    "-privacy": "privacy",
    "-p": "privacy",
    "-alt": "alt_text",
}

# Legacy opt-out pattern (inspired by the original "Archivist, no" bot)
LEGACY_OPTOUT = re.compile(r"archivist,?\s+no\b", re.IGNORECASE)


@dataclass
class ParsedMessage:
    """Result of parsing a Discord message for Archivist."""
    urls: List[str] = field(default_factory=list)
    commentary: str = ""
    no_summary: bool = False
    no_tags: bool = False
    no_archive: bool = False
    privacy: bool = False  # privacy = no_summary + no_tags
    alt_text: bool = False # explicit request for image alt-text
    opt_out: bool = False  # full opt-out (legacy "Archivist, no")

    @property
    def effective_no_summary(self) -> bool:
        return self.no_summary or self.privacy

    @property
    def effective_no_tags(self) -> bool:
        return self.no_tags or self.privacy

    @property
    def should_process(self) -> bool:
        return bool(self.urls) and not self.opt_out


def parse_message(content: str) -> ParsedMessage:
    """
    Parse a Discord message into URLs, flags, and commentary.
    """
    result = ParsedMessage()

    # Check for legacy opt-out
    if LEGACY_OPTOUT.search(content):
        result.opt_out = True
        # Still extract URLs for the record
        result.urls = URL_PATTERN.findall(content)
        return result

    # Extract URLs and filter out excluded ones
    raw_urls = URL_PATTERN.findall(content)
    result.urls = [
        url for url in raw_urls 
        if not any(pattern.search(url) for pattern in EXCLUDE_PATTERNS)
    ]

    # Strip URLs from content to find flags and commentary
    remaining = URL_PATTERN.sub("", content).strip()

    # Extract flags
    tokens = remaining.split()
    commentary_tokens = []
    for token in tokens:
        lower = token.lower()
        if lower in FLAGS:
            setattr(result, FLAGS[lower], True)
        else:
            commentary_tokens.append(token)

    result.commentary = " ".join(commentary_tokens).strip()

    return result
