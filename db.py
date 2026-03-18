"""Database models and connection management for Archivist."""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

import asyncpg

logger = logging.getLogger("archivist.db")

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS tags (
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

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

CREATE TABLE IF NOT EXISTS link_tags (
    link_id INTEGER REFERENCES archived_links(id) ON DELETE CASCADE,
    tag_id INTEGER REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (link_id, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_links_url ON archived_links(url);
CREATE INDEX IF NOT EXISTS idx_links_channel ON archived_links(channel_id);
CREATE INDEX IF NOT EXISTS idx_links_author ON archived_links(author_id);
CREATE INDEX IF NOT EXISTS idx_links_created ON archived_links(created_at DESC);
"""


class Database:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.pool: Optional[asyncpg.Pool] = None

    async def connect(self):
        """Create connection pool and initialize schema."""
        self.pool = await asyncpg.create_pool(
            host=self.config["host"],
            port=self.config["port"],
            database=self.config["database"],
            user=self.config["user"],
            password=self.config["password"],
            min_size=2,
            max_size=10,
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

    async def get_tag_pool(self) -> List[str]:
        """Return all known tags."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT name FROM tags ORDER BY name")
        return [r["name"] for r in rows]

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
        summary_skipped: bool = False,
        tags_skipped: bool = False,
        archive_skipped: bool = False,
        privacy_mode: bool = False,
    ) -> int:
        """Save an archived link and its tags. Returns the link ID."""
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
                    ) RETURNING id
                    """,
                    url, title, genre, gloss, summary, commentary, original_message,
                    archive_url, archive_service, snapshot_path, embedding,
                    guild_id, channel_id, message_id, author_id, bot_message_id,
                    summary_skipped, tags_skipped, archive_skipped, privacy_mode,
                    datetime.now(timezone.utc) if archive_url else None,
                )

                if tags:
                    tag_mapping = await self.ensure_tags(tags)
                    if tag_mapping:
                        await conn.executemany(
                            "INSERT INTO link_tags (link_id, tag_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                            [(link_id, tid) for tid in tag_mapping.values()],
                        )

        logger.info(f"Saved link {link_id}: {url}")
        return link_id

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
        """Return links from the last 24 hours with user/channel metadata."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT url, title, genre, gloss, summary, commentary, 
                       channel_id, author_id, created_at
                FROM archived_links
                WHERE created_at > NOW() - INTERVAL '24 hours'
                ORDER BY created_at ASC
                """
            )
        return [dict(r) for r in rows]
