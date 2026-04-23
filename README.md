# Archivist 📚

A Discord bot that watches channels for links and builds a searchable archive with AI-generated summaries, auto-tags, web archive snapshots, and PDF captures.

## Features

- **Link detection** — processes every URL posted in watched channels; Discord invites, already-archived URLs, and rate-limited users are handled gracefully
- **Content extraction** — fetches pages, extracts readable text via Mozilla Readability + `strip-tags`; YouTube URLs use the transcript API instead
- **Image understanding** — Gemini multimodal: describes images and transcribes visible text from attachments or linked image URLs
- **AI summaries** — Gemini produces a one-line gloss and a 2–3 sentence genre-aware summary (fiction, technical, essay, social media, etc.)
- **Auto-tagging** — Gemini suggests tags from a curated pool; new suggestions enter a proposal queue and are promoted once they exceed a use threshold (or immediately with `!promote-tag`)
- **Web archival** — submits to archive.is and Wayback Machine, uses whichever succeeds first
- **PDF snapshots** — full-page Playwright captures stored locally and optionally uploaded to the channel
- **Dual search** — `!search` tries semantic vector search (pgvector) first, falls back to full-text (`tsvector` + GIN index) if no embedding is available
- **Daily digest** — sends a Markdown summary of the day's archived links to the owner as a DM; windowed by last send time so no overlap or gap
- **SSRF protection** — every outbound fetch validates the target IP before connecting; manual redirect loop re-validates every hop
- **Rate limiting** — per-user token bucket (configurable, owner exempt); quiet 🐢 reaction on limit, no noisy error messages
- **Per-message flags** — inline control over summary, tags, archive, and privacy
- **Role-based overrides** — per-server, per-role, and per-channel config hierarchy; `ignore: true` on a role makes Archivist completely invisible to that user
- **Homelab-ready** — `uv`-managed dependencies, K3s `deployment.yaml`, secrets never committed

## Quick Start

### Prerequisites

- Python 3.12+ (managed by [`uv`](https://docs.astral.sh/uv/))
- PostgreSQL 14+ with the `pgvector` extension available  
  *(the bot installs it on first connect, but the server must permit `CREATE EXTENSION vector`)*
- Google Gemini API key ([Google AI Studio](https://aistudio.google.com/))
- Discord bot token with two **Privileged Gateway Intents** enabled in the  
  [Discord Developer Portal](https://discord.com/developers) → Bot → Privileged Gateway Intents:
  - **Message Content** — required to read message text
  - **Server Members** — required for role-based overrides (including the "Archivist ignore" role)
- Permissions: `Send Messages`, `Embed Links`, `Attach Files`, `Read Message History`

### Setup

```bash
# Install dependencies
uv sync

# Configure
cp config/default.yaml config/config.yaml
# Edit config/config.yaml — set discord.token, gemini.api_key, and database.*

# (Optional) create the database if it doesn't exist
psql -h localhost -U postgres -c "CREATE DATABASE archivist;"

# Run
uv run python bot.py
```

The bot creates all tables and installs the `vector` extension on first connect.

## Configuration

All settings live in `config/config.yaml` (copy from `config/default.yaml`). Key fields:

| Field | Description |
|-------|-------------|
| `discord.token` | Bot token |
| `discord.watched_channels` | List of channel snowflakes to watch (empty = all channels) |
| `discord.owner_id` | Snowflake of the user who receives digests and can use owner commands |
| `discord.digest_time` | `HH:MM` time to send the daily digest |
| `discord.timezone` | IANA timezone name (e.g. `America/New_York`); blank = UTC |
| `gemini.api_key` | Gemini API key |
| `gemini.model` | Model name (default: `gemini-2.0-flash`) |
| `rate_limit.per_minute` | Max URLs processed per user per minute (default: 5) |
| `rate_limit.per_hour` | Max URLs processed per user per hour (default: 30) |
| `archive.snapshots.enabled` | Whether to take Playwright PDF snapshots |

Environment variable overrides: `DISCORD_TOKEN`, `GEMINI_API_KEY`, `DB_PASSWORD`, `DB_HOST`.

### Role-based overrides

The `overrides` section in `config.yaml` lets you tune behavior per server, per role, and per channel. The hierarchy is: **defaults < server < role < channel** (channel wins).

```yaml
overrides:
  servers:
    "123456789012345678":         # guild snowflake
      roles:
        "987654321098765432":     # "Archivist-ignore" role — completely silent
          ignore: true
        "111222333444555666":     # power-user role — always archive, no summary
          summary: false
          archive: true
  channels:
    "555666777888999000":         # never tag anything in this channel
      tags: false
```

Role overrides require the **Server Members** privileged intent.

## Usage

Post any link in a watched channel — Archivist handles the rest:

```
https://simonwillison.net/2024/Jan/26/strip-tags/
```

### Per-message flags

Add flags anywhere in your message to control this specific post:

| Flag | Short | Effect |
|------|-------|--------|
| `-nosummary` | `-ns` | Skip AI summary |
| `-notags` | `-nt` | Skip auto-tagging |
| `-noarchive` | `-na` | Skip web archival |
| `-privacy` | `-p` | Skip summary AND tags |

**"Archivist, no"** anywhere in the message skips all processing (legacy opt-out).

### Owner commands

| Command | Effect |
|---------|--------|
| `!search <query>` | Search the archive (semantic → full-text fallback) |
| `!promote-tag <name>` | Promote a pending tag proposal to the curated pool |

## Architecture

```
Discord message
    │
    ├─ parser.py      Extract URLs, flags, commentary
    │
    ├─ net.py         SSRF guard — validate every URL and redirect hop
    │
    ├─ fetcher.py     Fetch page → readability-lxml → strip-tags
    │   youtube.py    YouTube URL → transcript API
    │
    ├─ (concurrent)
    │   archiver.py   archive.is → Wayback Machine fallback
    │   ai.py         Gemini: classify+tag → summary → embedding
    │   snapshot.py   Playwright PDF capture
    │
    ├─ formatter.py   Build Discord message (2000-char safe)
    └─ db.py          PostgreSQL: save link, tags, embedding, meta
```

### Database tables

| Table | Purpose |
|-------|---------|
| `archived_links` | One row per archived URL; includes embedding vector and generated `fts` column |
| `tags` | Curated tag pool fed back to Gemini |
| `tag_proposals` | LLM-suggested new tags with use counts; promoted to `tags` at threshold |
| `link_tags` | Many-to-many join between links and curated tags |
| `meta` | Key-value store for operational bookmarks (e.g. `last_digest_sent_at`) |

## Deployment

See [`DEPLOYMENT.md`](DEPLOYMENT.md) for full instructions and [`DATABASE.md`](DATABASE.md) for database setup, pgvector installation, and embedding internals. Short version:

```bash
# 1. Create the secret (never commit real values)
kubectl create secret generic archivist-secrets \
  --namespace archivist \
  --from-literal=DISCORD_TOKEN='...' \
  --from-literal=GEMINI_API_KEY='...' \
  --from-literal=DB_PASSWORD='...'

# 2. Build and push the image
docker build -t your-registry/archivist:latest .
docker push your-registry/archivist:latest

# 3. Apply
kubectl apply -f deployment.yaml
```

The `deployment.yaml` uses the `pgvector/pgvector:pg16` image for PostgreSQL so the `vector` extension is available without any manual setup.

## Testing

```bash
uv run pytest
```

61 tests covering the SSRF guard, message parser, formatter length limits, and YouTube URL patterns.
