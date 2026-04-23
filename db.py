"""Database models and connection management for Archivist."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any

import asyncpg
from pgvector.asyncpg import register_vector

logger = logging.getLogger("archivist.db")

# The vector extension must exist before the pool opens, because each pooled
# connection runs `register_vector` in its init callback and that requires the
# `vector` type to be resolvable at codec-registration time.
SCHEMA_EXTENSION = "CREATE EXTENSION IF NOT EXISTS vector;"

SCHEMA = """
-- ── Curated tag pool ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tags (
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── Pending tag proposals (LLM new_tags) ─────────────────────────────────────
-- Tags move from here into `tags` once they've been used enough times, or via
-- an explicit `!promote-tag <name>` command from the owner.
CREATE TABLE IF NOT EXISTS tag_proposals (
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    use_count INT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    last_used_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── Archived links ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS archived_links (
    id SERIAL PRIMARY KEY,
    url TEXT NOT NULL,
    title TEXT,
    genre TEXT,
    gloss TEXT,
    summary TEXT,
    commentary TEXT,
    original_message TEXT,
    archive_url TEXT,
    archive_service TEXT,
    snapshot_path TEXT,
    embedding vector(768),

    -- Discord metadata
    guild_id BIGINT,
    channel_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    author_id BIGINT NOT NULL,
    bot_message_id BIGINT,

    -- Processing flags
    summary_skipped BOOLEAN DEFAULT FALSE,
    tags_skipped BOOLEAN DEFAULT FALSE,
    archive_skipped BOOLEAN DEFAULT FALSE,
    privacy_mode BOOLEAN DEFAULT FALSE,

    -- Timestamps
    created_at TIMESTAMPTZ DEFAULT NOW(),
    archived_at TIMESTAMPTZ
);

-- Full-text search column (generated, so it stays in sync automatically).
-- Uses ALTER TABLE / ADD COLUMN IF NOT EXISTS so it's idempotent on existing DBs.
ALTER TABLE archived_links
    ADD COLUMN IF NOT EXISTS fts tsvector
    GENERATED ALWAYS AS (
        to_tsvector('english',
            coalesce(title,      '') || ' ' ||
            coalesce(gloss,      '') || ' ' ||
            coalesce(summary,    '') || ' ' ||
            coalesce(commentary, '')
        )
    ) STORED;

CREATE TABLE IF NOT EXISTS link_tags (
    link_id INTEGER REFERENCES archived_links(id) ON DELETE CASCADE,
    tag_id  INTEGER REFERENCES tags(id)           ON DELETE CASCADE,
    PRIMARY KEY (link_id, tag_id)
);

-- ── Key-value metadata store ──────────────────────────────────────────────────
-- Used for operational bookmarks like `last_digest_sent_at`.
CREATE TABLE IF NOT EXISTS meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── Indexes ───────────────────────────────────────────────────────────────────
-- `archived_links_url_unique` doubles as the url-lookup index AND enforces
-- dedupe. On a pre-existing DB with duplicate URLs this will fail loudly —
-- intentional: dedupe them by hand before re-running.
DROP INDEX IF EXISTS idx_links_url;
CREATE UNIQUE INDEX IF NOT EXISTS archived_links_url_unique ON archived_links(url);
CREATE INDEX IF NOT EXISTS idx_links_channel ON archived_links(channel_id);
CREATE INDEX IF NOT EXISTS idx_links_author  ON archived_links(author_id);
CREATE INDEX IF NOT EXISTS idx_links_created ON archived_links(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_links_fts     ON archived_links USING GIN(fts);
"""


class Database:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.pool: Optional[asyncpg.Pool] = None

    async def connect(self):
        """Create connection pool and initialize schema.

        Two-phase bootstrap so the pgvector codec can be registered on every
        pooled connection: first ensure the extension exists via a disposable
        connection, then open the pool with an init callback that registers
        the `vector` type.
        """
        conn_kwargs = dict(
            host=self.config["host"],
            port=self.config["port"],
            database=self.config["database"],
            user=self.config["user"],
            password=self.config["password"],
        )

        # Phase 1: ensure the vector extension exists.
        bootstrap = await asyncpg.connect(**conn_kwargs)
        try:
            await bootstrap.execute(SCHEMA_EXTENSION)
        finally:
            await bootstrap.close()

        # Phase 2: open the pool with the vector codec registered per-connection.
        async def _init_conn(conn: asyncpg.Connection) -> None:
            await register_vector(conn)

        self.pool = await asyncpg.create_pool(
            **conn_kwargs,
            min_size=2,
            max_size=10,
            init=_init_conn,
        )
        async with self.pool.acquire() as conn:
            await conn.execute(SCHEMA)
        logger.info("Database connected and schema initialized")

    async def close(self):
        if self.pool:
            await self.pool.close()

    async def seed_tags(self, tags: List[str]):
        """Insert seed tags, ignoring duplicates."""
        if not tags:
            return
        async with self.pool.acquire() as conn:
            await conn.executemany(
                "INSERT INTO tags (name) VALUES ($1) ON CONFLICT (name) DO NOTHING",
                [(t.lower().strip(),) for t in tags],
            )
        logger.info(f"Seeded {len(tags)} tags")

    async def get_tag_pool(self, proposal_threshold: int = 3) -> List[str]:
        """Return the curated tag pool plus any proposals used often enough.

        `proposal_threshold` controls how many times a proposed tag must appear
        before it surfaces in the pool fed back to the LLM.  The default of 3
        prevents one-off LLM hallucinations from polluting the vocabulary.
        """
        async with self.pool.acquire() as conn:
            curated = await conn.fetch("SELECT name FROM tags ORDER BY name")
            promoted = await conn.fetch(
                "SELECT name FROM tag_proposals WHERE use_count >= $1 ORDER BY name",
                proposal_threshold,
            )
        names = {r["name"] for r in curated} | {r["name"] for r in promoted}
        return sorted(names)

    async def record_proposals(self, tag_names: List[str]) -> None:
        """Record LLM-proposed tags in the proposals table.

        Each call increments `use_count` for existing entries so the most
        frequently suggested tags naturally bubble up to the curated pool.
        """
        if not tag_names:
            return
        async with self.pool.acquire() as conn:
            for name in tag_names:
                clean = name.lower().strip()
                if not clean:
                    continue
                await conn.execute(
                    """
                    INSERT INTO tag_proposals (name, use_count, last_used_at)
                    VALUES ($1, 1, NOW())
                    ON CONFLICT (name) DO UPDATE
                        SET use_count    = tag_proposals.use_count + 1,
                            last_used_at = NOW()
                    """,
                    clean,
                )

    async def promote_tag(self, name: str) -> bool:
        """Move a proposal into the curated `tags` table.

        Returns True if the tag was promoted (it existed in proposals or is new),
        False if it was already in the curated pool.
        """
        clean = name.lower().strip()
        if not clean:
            return False
        async with self.pool.acquire() as conn:
            already = await conn.fetchval(
                "SELECT id FROM tags WHERE name = $1", clean
            )
            if already is not None:
                return False
            await conn.execute(
                "INSERT INTO tags (name) VALUES ($1) ON CONFLICT (name) DO NOTHING",
                clean,
            )
            # Remove from proposals if present (keeps the tables consistent).
            await conn.execute(
                "DELETE FROM tag_proposals WHERE name = $1", clean
            )
        return True

    async def ensure_tags(self, tag_names: List[str]) -> Dict[str, int]:
        """Ensure tags exist, create if needed. Returns {name: id} mapping."""
        result = {}
        async with self.pool.acquire() as conn:
            for name in tag_names:
                clean_name = name.lower().strip()
                if not clean_name:
                    continue
                row = await conn.fetchrow(
                    """
                    INSERT INTO tags (name) VALUES ($1)
                    ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name
                    RETURNING id
                    """,
                    clean_name,
                )
                if row:
                    result[clean_name] = row["id"]
        return result

    async def save_link(
        self,
        url: str,
        title: Optional[str],
        genre: Optional[str],
        gloss: Optional[str],
        summary: Optional[str],
        commentary: Optional[str],
        original_message: Optional[str],
        archive_url: Optional[str],
        archive_service: Optional[str],
        snapshot_path: Optional[str],
        embedding: Optional[List[float]],
        guild_id: Optional[int],
        channel_id: int,
        message_id: int,
        author_id: int,
        bot_message_id: Optional[int],
        tags: Optional[List[str]],
        proposed_tags: Optional[List[str]] = None,
        summary_skipped: bool = False,
        tags_skipped: bool = False,
        archive_skipped: bool = False,
        privacy_mode: bool = False,
    ) -> Optional[int]:
        """Save an archived link and its curated tags.

        `tags` are the curated tags (from the known pool) — they get saved to
        `link_tags`.  `proposed_tags` are new LLM suggestions — they go to
        `tag_proposals` for counting; once a proposal's use_count reaches the
        threshold it surfaces in the tag pool fed back to the LLM.

        Returns the link ID, or None if a concurrent task already saved this URL
        (the pre-flight dedupe in `bot.process_link` closes the gap to
        microseconds, but `ON CONFLICT` makes the write truly idempotent).
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                link_id = await conn.fetchval(
                    """
                    INSERT INTO archived_links (
                        url, title, genre, gloss, summary, commentary, original_message,
                        archive_url, archive_service, snapshot_path, embedding,
                        guild_id, channel_id, message_id, author_id, bot_message_id,
                        summary_skipped, tags_skipped, archive_skipped, privacy_mode,
                        archived_at
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14,
                        $15, $16, $17, $18, $19, $20, $21
                    )
                    ON CONFLICT (url) DO NOTHING
                    RETURNING id
                    """,
                    url, title, genre, gloss, summary, commentary, original_message,
                    archive_url, archive_service, snapshot_path, embedding,
                    guild_id, channel_id, message_id, author_id, bot_message_id,
                    summary_skipped, tags_skipped, archive_skipped, privacy_mode,
                    datetime.now(timezone.utc) if archive_url else None,
                )

                if link_id is None:
                    logger.info(f"Skipped duplicate save: {url}")
                    return None

                if tags:
                    tag_mapping = await self.ensure_tags(tags)
                    if tag_mapping:
                        await conn.executemany(
                            "INSERT INTO link_tags (link_id, tag_id) VALUES ($1, $2)"
                            " ON CONFLICT DO NOTHING",
                            [(link_id, tid) for tid in tag_mapping.values()],
                        )

        # Record proposals outside the transaction — a failure here is non-fatal.
        if proposed_tags:
            try:
                await self.record_proposals(proposed_tags)
            except Exception:
                logger.exception("Failed to record tag proposals (non-fatal)")

        logger.info(f"Saved link {link_id}: {url}")
        return link_id

    async def find_existing_link(self, url: str) -> Optional[Dict[str, Any]]:
        """Return the existing archive row for `url`, or None.

        Used for the pre-flight dedupe check — we short-circuit before fetch /
        LLM / archive work if we've already archived this URL.
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, url, title, channel_id, guild_id, bot_message_id, created_at
                FROM archived_links
                WHERE url = $1
                ORDER BY id ASC
                LIMIT 1
                """,
                url,
            )
        return dict(row) if row else None

    async def search_links(self, query_embedding: List[float], limit: int = 5) -> List[Dict[str, Any]]:
        """Perform vector similarity search."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT url, title, gloss, summary, archive_url
                FROM archived_links
                ORDER BY embedding <=> $1
                LIMIT $2
                """,
                query_embedding,
                limit
            )
        return [dict(r) for r in rows]

    async def get_daily_links(self) -> List[Dict[str, Any]]:
        """Return links from the last 24 hours (fallback for callers that don't
        use the windowed version)."""
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        return await self.get_links_since(since)

    async def get_links_since(self, since: datetime) -> List[Dict[str, Any]]:
        """Return links archived after *since* with user/channel metadata."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT url, title, genre, gloss, summary, commentary,
                       channel_id, author_id, created_at
                FROM archived_links
                WHERE created_at > $1
                ORDER BY created_at ASC
                """,
                since,
            )
        return [dict(r) for r in rows]

    # ── Meta key-value store ─────────────────────────────────────────────────

    async def get_meta(self, key: str) -> Optional[str]:
        """Return the stored string value for *key*, or None if not set."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT value FROM meta WHERE key = $1", key
            )
        return row["value"] if row else None

    async def set_meta(self, key: str, value: str) -> None:
        """Upsert a key-value pair in the meta table."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO meta (key, value, updated_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (key) DO UPDATE
                    SET value = EXCLUDED.value,
                        updated_at = NOW()
                """,
                key,
                value,
            )

    # ── Full-text search ─────────────────────────────────────────────────────

    async def search_links_text(
        self, query: str, limit: int = 5
    ) -> List[Dict[str, Any]]:
        """Postgres full-text search over title, gloss, summary, and commentary.

        Returns results ranked by `ts_rank`, most relevant first.  Returns an
        empty list (not an error) if the `fts` column doesn't exist yet on a
        pre-migration DB — callers can fall back to vector search in that case.
        """
        async with self.pool.acquire() as conn:
            try:
                rows = await conn.fetch(
                    """
                    SELECT url, title, gloss, summary, archive_url,
                           ts_rank(fts, plainto_tsquery('english', $1)) AS rank
                    FROM archived_links
                    WHERE fts @@ plainto_tsquery('english', $1)
                    ORDER BY rank DESC
                    LIMIT $2
                    """,
                    query,
                    limit,
                )
            except Exception:
                logger.exception("Full-text search failed")
                return []
        return [dict(r) for r in rows]
