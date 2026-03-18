# Archivist 📚

A Discord bot that watches channels for links and creates rich archive entries with AI-generated summaries, auto-tags, and web archive snapshots.

## Features

- **Link detection** — automatically processes any URL posted in watched channels
- **Content extraction** — fetches pages and extracts readable text via Mozilla Readability + Simon Willison's `strip-tags`
- **AI summaries** — Google Gemini (1.5 Flash) generates a one-line gloss and 2-3 sentence summary
- **Auto-tagging** — Google Gemini suggests tags from your growing pool
- **Web archival** — snapshots via archive.is and Wayback Machine
- **PostgreSQL storage** — full-text searchable catalog of everything you've saved
- **Pseudo-CLI flags** — per-message control over what the bot does
- **K3s Native** — designed for homelab deployment with `uv` and PostgreSQL

## Quick Start

### Prerequisites

- Python 3.12+ (managed by `uv`)
- PostgreSQL
- Google Gemini API key
- Discord bot token with Message Content intent

### Setup

```bash
# Install dependencies
uv sync

# Configure
cp config/default.yaml config/config.yaml
# Edit config.yaml with your tokens, DB credentials, etc.

# Run
uv run python bot.py
```

## Usage

Just post links in a watched channel:

```
https://simonwillison.net/2024/Jan/26/strip-tags/
```

### Flags

Add flags anywhere in your message to control behavior:

| Flag | Short | Effect |
|------|-------|--------|
| `-nosummary` | `-ns` | Skip AI summary |
| `-notags` | `-nt` | Skip auto-tagging |
| `-noarchive` | `-na` | Skip web archival |
| `-privacy` | `-p` | Skip summary AND tags |

Legacy opt-out: **"Archivist, no"** anywhere in the message to skip processing.

## Architecture

```
Discord message
    ↓
parser.py    → Extract URLs, flags, commentary
    ↓
fetcher.py   → Fetch + readability-lxml + strip-tags
    ↓ (concurrent)
archiver.py  → archive.is → Wayback fallback
ai.py        → Gemini summary → Gemini tags
    ↓
formatter.py → Markdown Discord message
db.py        → PostgreSQL storage
```

## Deployment

A `deployment.yaml` is provided for K3s. It includes a StatefulSet for PostgreSQL and a Deployment for the bot.

```bash
# Update secrets in deployment.yaml, then:
kubectl apply -f deployment.yaml
```

The bot also supports environment variable overrides:

- `DISCORD_TOKEN`
- `GEMINI_API_KEY`
- `DB_PASSWORD`
- `DB_HOST`
