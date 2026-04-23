# Database & Embeddings Setup

This document covers everything below the `CREATE DATABASE archivist;` line:
how pgvector gets installed, what the schema looks like and why, how embeddings
are generated and stored, and what to do when things go sideways.

---

## 1. PostgreSQL + pgvector

### What you need

| Requirement | Notes |
|-------------|-------|
| PostgreSQL 14+ | 14 for generated columns (`fts tsvector GENERATED ALWAYS AS … STORED`) |
| pgvector extension | Must be installable by the connecting user — the bot issues `CREATE EXTENSION IF NOT EXISTS vector` on first connect |

**The bot does not need superuser.** It only needs `CREATE EXTENSION` privilege
on the target database, which a database owner has by default:

```sql
-- If you're connecting as a non-owner, grant it explicitly:
GRANT CREATE ON DATABASE archivist TO archivist_user;
```

### Installing pgvector

**K3s / Docker** — use the official image; no manual install needed:

```yaml
# deployment.yaml already uses:
image: pgvector/pgvector:pg16
```

**macOS (Homebrew):**

```bash
brew install pgvector
# then in psql:
CREATE EXTENSION vector;
```

**Debian / Ubuntu:**

```bash
sudo apt install postgresql-16-pgvector   # adjust version number
```

**Compile from source (any platform):**

```bash
git clone https://github.com/pgvector/pgvector.git
cd pgvector
make
make install   # requires pg_config on PATH
```

**Verify installation:**

```sql
SELECT * FROM pg_available_extensions WHERE name = 'vector';
-- Should return one row. If empty, the .so isn't installed yet.
```

### What the bot does on startup

The connection sequence in `db.py` is deliberately two-phase to avoid a
chicken-and-egg problem with the asyncpg type codec:

1. **Bootstrap connection** (single, non-pooled): runs
   `CREATE EXTENSION IF NOT EXISTS vector` — this must succeed before the pool
   opens, because every pooled connection registers the `vector` codec type on
   init, and that registration requires the type to already exist in the
   catalog.

2. **Pool open** (2–10 connections): each new connection runs
   `register_vector(conn)` from the `pgvector.asyncpg` package, which teaches
   asyncpg how to encode/decode `vector(768)` values as Python lists of floats.

3. **Schema init**: runs `SCHEMA` DDL against the pool. Every statement is
   `CREATE TABLE IF NOT EXISTS` or `CREATE INDEX IF NOT EXISTS` or
   `ALTER TABLE … ADD COLUMN IF NOT EXISTS`, so re-running on an existing
   database is safe and idempotent.

If step 1 fails (pgvector not installed), the bot exits immediately with a
clear error. Nothing in the channel will break silently — the startup sequence
is fail-fast.

---

## 2. Schema overview

```
archived_links          Core archive — one row per URL
  ├── embedding         vector(768) for semantic search
  └── fts               tsvector GENERATED ALWAYS (full-text search)

tags                    Curated tag vocabulary fed back to Gemini
tag_proposals           LLM-suggested new tags, with use counts
link_tags               Many-to-many join: archived_links ↔ tags

meta                    Key-value store for operational bookmarks
```

### `archived_links`

The main table. Key columns:

| Column | Type | Notes |
|--------|------|-------|
| `url` | TEXT | Unique index (`archived_links_url_unique`) — also the dedupe key |
| `embedding` | vector(768) | NULL if the link was archived with `-nosummary`, `-privacy`, or if Gemini was unavailable |
| `fts` | tsvector | Generated from `title \|\| gloss \|\| summary \|\| commentary`; stays in sync automatically |
| `summary_skipped` | BOOLEAN | True when `-nosummary` or `-privacy` was used |
| `privacy_mode` | BOOLEAN | True when `-privacy` was used |
| `archived_at` | TIMESTAMPTZ | Set only when an `archive_url` was recorded |

### `tags` and `tag_proposals`

Gemini returns two sets of tags per link:
- **`tags`** — picked from the pool it was given (curated)
- **`new_tags`** — invented by the model (proposals)

Curated tags are linked to the archived link via `link_tags`. Proposals are
recorded in `tag_proposals` with a `use_count` that increments each time the
model suggests the same tag. Once `use_count` reaches the threshold
(default: 3), the tag surfaces in the pool fed back to the model on future
calls. The owner can also promote a proposal immediately with
`!promote-tag <name>`.

This prevents one-off hallucinations from polluting the vocabulary while still
letting genuinely useful new terms organically graduate.

### `meta`

A simple key-value table. Currently used for one key:

| Key | Value | Purpose |
|-----|-------|---------|
| `last_digest_sent_at` | ISO 8601 datetime | Window start for the next daily digest |

The digest queries `created_at > last_digest_sent_at` rather than
`created_at > NOW() - INTERVAL '24 hours'`, so no links are double-counted if
the digest fires slightly early, and none are missed if it fires slightly late.

---

## 3. Embeddings

### What they are

Each archived link (that has a summary) gets a 768-dimensional float vector
stored in `archived_links.embedding`. This vector encodes the semantic meaning
of the content, not just its keywords, which is why `!search quantum entanglement`
can surface a physics article even if those exact words aren't in the title.

### The model

| Parameter | Value |
|-----------|-------|
| Model | `text-embedding-004` (Google) |
| Dimensions | 768 |
| Task type | `retrieval_document` (for storage); `retrieval_query` is used implicitly by the API for search queries |
| Column type | `vector(768)` |

The model name is hardcoded in `ai.generate_embedding`. It is separate from
`gemini.model` in `config.yaml` (which controls summarisation) because the
embedding model is not a generative model and doesn't change with the Flash/Pro
tier selection.

### What gets embedded

An embedding is generated only when:
1. The content extraction produced enough text (> 50 chars)
2. A summary was successfully generated (not skipped via `-nosummary` / `-privacy`)
3. The Gemini API call succeeded

The text fed to the embedding model is a compact representation of the
archival result:

```
Title: <page title>
Gloss: <one-line subject description>
Summary: <2–3 sentence summary>
Context: <original Discord message content>
```

Including the Discord message content means the user's own words about a link
— "this is the best explanation of X I've ever read" — influence the embedding
and make it retrievable via their own vocabulary.

Links archived with `-privacy` or `-nosummary`, and links whose summary failed,
will have `embedding = NULL`. They are still retrievable via full-text search.

### How search uses embeddings

When `!search <query>` is issued:

1. The query text is embedded using the same `text-embedding-004` model (same
   768 dimensions).
2. pgvector finds the 5 nearest neighbours using the cosine distance operator
   (`<=>`) — rows with NULL embeddings are excluded automatically.
3. If no results come back (or if the Gemini API is unavailable), the bot falls
   back to `plainto_tsquery` full-text search over the `fts` generated column.

The fallback means search keeps working even with no API key or during a Gemini
outage — it just loses the semantic layer.

---

## 4. Operational notes

### Backfilling embeddings for old links

If links were archived before the Gemini API key was configured, or before the
embedding call was working, they'll have `embedding = NULL`. You can backfill
them by querying the rows and re-running `generate_embedding` against each
one's `gloss || summary` text, then updating. There's no built-in command for
this yet — for a small archive it's a one-off script:

```python
# Rough sketch — run outside the bot
import asyncio, asyncpg
from ai import generate_embedding

async def backfill(api_key, dsn):
    pool = await asyncpg.create_pool(dsn)
    rows = await pool.fetch(
        "SELECT id, title, gloss, summary FROM archived_links "
        "WHERE embedding IS NULL AND summary IS NOT NULL"
    )
    for row in rows:
        text = f"Title: {row['title'] or ''}\nGloss: {row['gloss'] or ''}\nSummary: {row['summary']}"
        emb = await generate_embedding(text, api_key)
        if emb:
            await pool.execute(
                "UPDATE archived_links SET embedding = $1 WHERE id = $2",
                emb, row['id']
            )
            print(f"  embedded #{row['id']}")
    await pool.close()
```

### Changing the embedding model

If you switch to a model with different output dimensions:

1. Update the `model` default in `ai.generate_embedding`.
2. Drop and recreate the column:

   ```sql
   ALTER TABLE archived_links DROP COLUMN embedding;
   ALTER TABLE archived_links ADD COLUMN embedding vector(<new_dim>);
   ```

3. Drop and recreate the index (the bot will recreate it on next startup, but
   you can also run it manually):

   ```sql
   -- The bot's SCHEMA DDL doesn't yet have an explicit embedding index;
   -- add one manually if your archive is large:
   CREATE INDEX idx_links_embedding ON archived_links
       USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
   ```

4. Backfill (see above).

Note: text-embedding-004 at 768 dims is a good default. The next step up
(text-embedding-004 with 1536 dims, or text-multilingual-embedding-002) would
require the column change above.

### Checking extension and index health

```sql
-- Is pgvector installed?
SELECT extversion FROM pg_extension WHERE extname = 'vector';

-- How many links have embeddings vs. not?
SELECT
    COUNT(*) FILTER (WHERE embedding IS NOT NULL) AS with_embedding,
    COUNT(*) FILTER (WHERE embedding IS NULL)     AS without_embedding
FROM archived_links;

-- Tag pool health: curated vs. pending proposals
SELECT 'curated' AS pool, COUNT(*) FROM tags
UNION ALL
SELECT 'proposals', COUNT(*) FROM tag_proposals
UNION ALL
SELECT 'proposals_ready', COUNT(*) FROM tag_proposals WHERE use_count >= 3;

-- Inspect the fts column on a few rows
SELECT id, title, fts FROM archived_links LIMIT 3;
```

### Backups

```bash
# Local
pg_dump -U archivist archivist > backup_$(date +%F).sql

# K3s StatefulSet
kubectl exec -it statefulset/postgres -n archivist \
  -- pg_dump -U archivist archivist > backup_$(date +%F).sql
```

The `embedding` column serialises correctly through `pg_dump` / `pg_restore` —
pgvector registers its own output function and `COPY` format.

---

## 5. Quick-reference: first-run checklist

- [ ] PostgreSQL 14+ is running and reachable
- [ ] pgvector extension is installable (`pg_available_extensions` shows `vector`)
- [ ] `archivist` database exists (`CREATE DATABASE archivist`)
- [ ] Connecting user has `CREATE` on the database (or is the owner)
- [ ] `config/config.yaml` has correct `database.*` values
- [ ] Gemini API key is set (required for embeddings; full-text search works without it)
- [ ] Bot starts cleanly — look for `Database connected and schema initialized` in logs
