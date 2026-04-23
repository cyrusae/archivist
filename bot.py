"""
Archivist - A Discord bot for intelligent link archiving.
"""

import asyncio
import logging
import os
import re
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Optional, List, Set, Any, Dict
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
import discord
from discord.ext import tasks
import yaml
import uvloop

from ai import (
    classify_and_tag,
    generate_summary,
    describe_image,
    generate_embedding,
    ImageResult,
    SummaryResult,
    ClassificationResult
)
from archiver import archive_url, ArchiveResult
from db import Database
from fetcher import fetch_page, FetchedPage
from formatter import format_archive_message, _cap, _DISCORD_LIMIT
from net import ResponseTooLarge, UnsafeURLError, safe_get
from parser import parse_message, ParsedMessage
from snapshot import capture_snapshot, SnapshotResult
from youtube import fetch_transcript

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-20s %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("archivist")

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
SOCIAL_DOMAINS = {"x.com", "twitter.com", "bsky.app", "tumblr.com", "threads.net"}
VIDEO_DOMAINS = {"youtube.com", "youtu.be", "vimeo.com", "twitch.tv"}
# Domains for which we attempt a YouTube transcript fetch.
YOUTUBE_DOMAINS = {"youtube.com", "youtu.be", "m.youtube.com", "music.youtube.com"}

# Required config paths — startup fails fast with a readable message if these
# are missing, empty, or still carrying their `YOUR_*` placeholder values.
REQUIRED_CONFIG_KEYS: List[tuple] = [
    ("discord", "token"),
    ("gemini", "api_key"),
    ("database", "host"),
    ("database", "port"),
    ("database", "database"),
    ("database", "user"),
    ("database", "password"),
]


def _safe_error(e: BaseException) -> str:
    """Scrubbed error label for user-facing messages — full detail still logs."""
    return type(e).__name__


def _parse_owner_id(raw: Any) -> Optional[int]:
    """Accept only a positive decimal snowflake; anything else becomes None."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or not s.isdigit():
        return None
    return int(s)


def _validate_config(config: dict) -> None:
    """Fail fast with a readable message if required keys are missing or
    still carry the `YOUR_*` placeholders from `default.yaml`."""
    missing: List[str] = []
    placeholders: List[str] = []
    for section, key in REQUIRED_CONFIG_KEYS:
        val = config.get(section, {}).get(key) if isinstance(config.get(section), dict) else None
        if val is None or val == "":
            missing.append(f"{section}.{key}")
        elif isinstance(val, str) and val.startswith("YOUR_"):
            placeholders.append(f"{section}.{key}")
    if missing or placeholders:
        parts = []
        if missing:
            parts.append(f"missing keys: {', '.join(missing)}")
        if placeholders:
            parts.append(f"placeholder values still present: {', '.join(placeholders)}")
        logger.error("Config error: " + "; ".join(parts))
        sys.exit(1)


class RateLimiter:
    """In-memory per-user token bucket.

    Deliberately dumb: one deque of timestamps per user, pruned lazily on
    each check. Not durable across restarts (fine — the limits are generous
    enough that a restart effectively resets the cooldown, and we'd rather
    rebuild than carry stale state).
    """

    def __init__(self, per_minute: int = 5, per_hour: int = 30):
        self.per_minute = per_minute
        self.per_hour = per_hour
        self._events: Dict[int, List[float]] = {}

    def check(self, user_id: int) -> bool:
        """Record an attempt; return True if it's allowed, False if rate-limited."""
        import time as _time

        now = _time.monotonic()
        events = self._events.setdefault(user_id, [])
        # Prune events older than the longest window.
        cutoff = now - 3600
        if events and events[0] < cutoff:
            events[:] = [t for t in events if t > cutoff]

        if len(events) >= self.per_hour:
            return False
        minute_cutoff = now - 60
        if sum(1 for t in events if t > minute_cutoff) >= self.per_minute:
            return False
        events.append(now)
        return True


def _parse_digest_time(spec: str, tz_name: Optional[str]) -> time:
    """Parse an `HH:MM` string into a tz-aware `datetime.time`.

    `discord.ext.tasks.loop(time=...)` requires a tz-aware time to fire at a
    deterministic wall-clock moment. We default to UTC; opt into a named zone
    via `discord.timezone` in `config.yaml`.
    """
    try:
        h_str, m_str = spec.split(":", 1)
        h, m = int(h_str), int(m_str)
    except (ValueError, AttributeError):
        raise SystemExit(f"Config error: discord.digest_time must be HH:MM, got {spec!r}")
    if not (0 <= h < 24 and 0 <= m < 60):
        raise SystemExit(f"Config error: digest_time out of range: {spec!r}")
    tz: Any
    if tz_name:
        try:
            tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            raise SystemExit(f"Config error: unknown timezone {tz_name!r}")
    else:
        tz = timezone.utc
    return time(hour=h, minute=m, tzinfo=tz)

# ---------------------------------------------------------------------------
# Load config
# ---------------------------------------------------------------------------
def load_config(path: str = "config/config.yaml") -> dict:
    p = Path(path)
    if not p.exists():
        p = Path("config/default.yaml")
        if not p.exists():
            logger.error(f"Config file not found")
            sys.exit(1)
    
    with open(p) as f:
        config = yaml.safe_load(f)
    
    # Defaults for overrides if missing
    if "overrides" not in config: config["overrides"] = {}
    if "servers" not in config["overrides"]: config["overrides"]["servers"] = {}
    if "channels" not in config["overrides"]: config["overrides"]["channels"] = {}

    if "DISCORD_TOKEN" in os.environ:
        config["discord"]["token"] = os.environ["DISCORD_TOKEN"]
    if "GEMINI_API_KEY" in os.environ:
        config["gemini"]["api_key"] = os.environ["GEMINI_API_KEY"]
    if "DB_PASSWORD" in os.environ:
        config["database"]["password"] = os.environ["DB_PASSWORD"]
    if "DB_HOST" in os.environ:
        config["database"]["host"] = os.environ["DB_HOST"]

    _validate_config(config)
    return config


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------
class ArchivistBot(discord.Client):
    def __init__(self, config: dict):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(intents=intents)

        self.config = config
        self.db = Database(config["database"])
        self.watched_channels: Set[int] = set(config["discord"].get("watched_channels", []))
        self.owner_id: Optional[int] = _parse_owner_id(config["discord"].get("owner_id"))

        rl_cfg = config.get("rate_limit", {}) or {}
        self._rate_limiter = RateLimiter(
            per_minute=int(rl_cfg.get("per_minute", 5)),
            per_hour=int(rl_cfg.get("per_hour", 30)),
        )
        self._tasks: Set[asyncio.Task] = set()
        self._shutting_down = False

    def _spawn(self, coro, *, name: str) -> asyncio.Task:
        """Create a task we track so `close()` can drain it on shutdown."""
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def setup_hook(self):
        await self.db.connect()
        await self.db.seed_tags(self.config.get("seed_tags", []))

        # Daily digest: only schedule if we know who to send it to. Parse the
        # target time once here and reconfigure the loop before starting.
        if self.owner_id:
            digest_spec = self.config["discord"].get("digest_time", "08:00")
            tz_name = self.config["discord"].get("timezone")
            digest_time = _parse_digest_time(digest_spec, tz_name)
            self.digest_loop.change_interval(time=digest_time)
            self.digest_loop.start()
            logger.info(f"Daily digest scheduled at {digest_time}")

        logger.info("Archivist is ready.")

    async def close(self):
        """Drain in-flight archive tasks, then close the DB pool and gateway.

        Order matters: the DB pool must outlive every task that holds a
        connection, otherwise tasks mid-write will raise `InterfaceError:
        pool is closed`.
        """
        self._shutting_down = True

        pending = [t for t in self._tasks if not t.done()]
        if pending:
            logger.info(f"Draining {len(pending)} in-flight task(s)...")
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=30.0,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"Drain timeout — canceling {len(pending)} stuck task(s)"
                )
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

        await self.db.close()
        await super().close()

    # The `time=` is a placeholder; `setup_hook` calls `change_interval(time=...)`
    # with the real configured value before starting the loop.
    @tasks.loop(time=time(8, 0, tzinfo=timezone.utc))
    async def digest_loop(self):
        """Send a daily digest of all archived links to the owner.

        `tasks.loop(time=...)` fires once per day at the configured wall-clock
        time — no manual sleep needed and no drift across runs.
        """
        try:
            await self.send_daily_digest()
        except Exception:
            logger.exception("Failed to send daily digest")

    @digest_loop.before_loop
    async def _before_digest_loop(self):
        await self.wait_until_ready()

    async def send_daily_digest(self):
        """Generate and send the digest.

        Uses the `last_digest_sent_at` meta key to window the query: only links
        newer than the last successful digest are included.  This eliminates
        both overlap (links counted twice) and gap (links missed because the
        scheduler fired early) once the digest loop uses `tasks.loop(time=...)`.
        """
        if not self.owner_id:
            return

        last_str = await self.db.get_meta("last_digest_sent_at")
        if last_str:
            try:
                since = datetime.fromisoformat(last_str)
            except ValueError:
                since = datetime.now(timezone.utc) - timedelta(hours=24)
        else:
            since = datetime.now(timezone.utc) - timedelta(hours=24)

        links = await self.db.get_links_since(since)
        if not links:
            logger.info("No links found for daily digest.")
            return
            
        owner = await self.fetch_user(self.owner_id)
        if not owner:
            logger.error(f"Could not find owner with ID {self.owner_id}")
            return
            
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        content = [f"# 📚 Archivist Daily Digest - {date_str}\n"]
        for link in links:
            title = link.get("title") or "Untitled"
            url = link.get("url")
            author_id = link.get("author_id")
            channel_id = link.get("channel_id")
            gloss = link.get("gloss") or ""

            content.append(f"## {title}")
            content.append(f"- **URL:** {url}")
            content.append(f"- **Shared by:** <@{author_id}> in <#{channel_id}>")
            if gloss:
                content.append(f"- **Gloss:** {gloss}")
            content.append("")

        full_text = "\n".join(content)

        # Use a NamedTemporaryFile so concurrent invocations can't collide and
        # cleanup is guaranteed even if `owner.send` raises mid-upload.
        tmp = tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".md",
            prefix=f"archivist_digest_{date_str}_",
            delete=False,
            encoding="utf-8",
        )
        try:
            tmp.write(full_text)
            tmp.close()
            await owner.send(
                f"📊 Good morning! Here is your Archivist digest for {date_str}.",
                file=discord.File(
                    tmp.name, filename=f"archivist_digest_{date_str}.md"
                ),
            )
        finally:
            try:
                os.remove(tmp.name)
            except OSError:
                pass

        # Record only after a successful send — if owner.send raises, we don't
        # advance the window so the next run will retry the same time slice.
        await self.db.set_meta(
            "last_digest_sent_at",
            datetime.now(timezone.utc).isoformat(),
        )
        logger.info(f"Daily digest sent to {owner.name}")

    def get_overrides(self, message: discord.Message) -> Dict[str, Any]:
        """Apply hierarchical overrides: Global < Server < Channel < Role.

        A role override with `ignore: true` means Archivist should treat the
        author as if they never sent anything — no archiving, no reactions.
        It short-circuits the entire hierarchy: once any role sets `ignore`,
        the returned dict will have `ignore: True` regardless of what channel
        or other role overrides say.
        """
        res = self.config.get("defaults", {}).copy()
        ovr = self.config.get("overrides", {})

        if message.guild:
            sid = str(message.guild.id)
            if sid in ovr.get("servers", {}):
                res.update(ovr["servers"][sid])
                if isinstance(message.author, discord.Member):
                    for role in message.author.roles:
                        rid = str(role.id)
                        role_ovr = ovr["servers"][sid].get("roles", {}).get(rid)
                        if role_ovr:
                            res.update(role_ovr)
                            if role_ovr.get("ignore"):
                                # Bail out immediately — ignore overrides everything.
                                return {**res, "ignore": True}

        cid = str(message.channel.id)
        if cid in ovr.get("channels", {}):
            res.update(ovr["channels"][cid])

        return res

    def should_watch(self, channel_id: int) -> bool:
        if not self.watched_channels:
            return True
        return channel_id in self.watched_channels

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if self._shutting_down:
            # Stop accepting new work once close() has been entered.
            return

        # --- PREFIX COMMANDS (owner-only) ---
        if message.content.startswith("!search "):
            # Fail closed: if no owner is configured, or the author is anyone
            # other than the owner, silently drop the command. Never expose the
            # archive to arbitrary guild members.
            if self.owner_id is None or message.author.id != self.owner_id:
                return
            query = message.content[8:].strip()
            if query:
                await self.handle_search(message, query)
            return

        if message.content.startswith("!promote-tag "):
            if self.owner_id is None or message.author.id != self.owner_id:
                return
            tag_name = message.content[13:].strip().lower()
            if not tag_name:
                return
            try:
                promoted = await self.db.promote_tag(tag_name)
                if promoted:
                    await message.channel.send(
                        f"✅ Tag `{tag_name}` promoted to the curated pool."
                    )
                else:
                    await message.channel.send(
                        f"ℹ️ Tag `{tag_name}` is already in the curated pool."
                    )
            except Exception as e:
                logger.exception(f"!promote-tag failed for {tag_name!r}")
                await message.channel.send(
                    f"⚠️ Promote failed ({_safe_error(e)}); check logs."
                )
            return
        # -----------------------------------

        if not self.should_watch(message.channel.id):
            return

        parsed = parse_message(message.content)
        
        # Apply Overrides
        overrides = self.get_overrides(message)
        if overrides.get("ignore"):
            # This author has a role configured with `ignore: true` — treat
            # their messages as invisible. No reaction, no logging noise.
            return
        if not overrides.get("summary", True): parsed.no_summary = True
        if not overrides.get("tags", True): parsed.no_tags = True
        if not overrides.get("archive", True): parsed.no_archive = True

        if not parsed.should_process and not message.attachments:
            return

        # Rate limit non-owners. One counted event per message (not per URL),
        # so a single post with three URLs isn't punitive.
        if message.author.id != self.owner_id and not self._rate_limiter.check(message.author.id):
            logger.info(
                f"Rate-limited author {message.author.id} in channel {message.channel.id}"
            )
            try:
                await message.add_reaction("🐢")
            except discord.HTTPException:
                pass
            return

        # Handle explicit attachments
        for attachment in message.attachments:
            if any(attachment.filename.lower().endswith(ext) for ext in IMAGE_EXTENSIONS):
                self._spawn(
                    self.process_image_attachment(message, attachment, parsed),
                    name=f"image-{attachment.id}",
                )

        # Handle URLs. Clone `parsed` per URL so social-domain flag overrides on
        # one URL can't leak into unrelated URLs in the same message.
        for url in parsed.urls:
            per_url = replace(parsed)

            domain = ""
            parsed_url = None
            try:
                parsed_url = urlparse(url)
                domain = parsed_url.netloc.lower()
                if domain.startswith("www."):
                    domain = domain[4:]
            except Exception:
                logger.warning(f"Failed to parse URL: {url!r}")

            if parsed_url is not None and domain == "archiveofourown.org":
                query = parse_qs(parsed_url.query)
                query["view_adult"] = ["true"]
                query["view_full_work"] = ["true"]
                url = urlunparse(
                    parsed_url._replace(query=urlencode(query, doseq=True))
                )

            if domain in SOCIAL_DOMAINS:
                per_url.no_summary = True
                per_url.no_tags = True

            if any(url.lower().split("?")[0].endswith(ext) for ext in IMAGE_EXTENSIONS):
                self._spawn(
                    self.process_image_url(message, url, per_url),
                    name=f"image-url-{url[:50]}",
                )
            else:
                self._spawn(
                    self.process_link(message, url, per_url, domain),
                    name=f"archive-{url[:50]}",
                )

    async def handle_search(self, message: discord.Message, query: str):
        """Handle search query: tries semantic (vector) search first, then
        falls back to full-text search if no vector results are found.

        Full-text search is always available even if Gemini embedding fails
        (e.g. no API key, quota exhausted).
        """
        async with message.channel.typing():
            try:
                api_key = self.config["gemini"]["api_key"]

                # --- Semantic search ---
                results: List[Dict[str, Any]] = []
                embedding = await generate_embedding(query, api_key)
                if embedding:
                    results = await self.db.search_links(embedding, limit=5)

                # --- Full-text fallback ---
                header = "### 🔍 Search Results"
                if not results:
                    results = await self.db.search_links_text(query, limit=5)
                    header = "### 🔍 Full-text Search Results"

                if not results:
                    await message.channel.send(
                        "📂 No relevant matches found in the archive."
                    )
                    return

                response = [header]
                for i, res in enumerate(results, 1):
                    title = _cap(res.get("title") or "Untitled", 100)
                    gloss = _cap(res.get("gloss") or "", 120)
                    url = res.get("url") or ""
                    response.append(f"{i}. **{title}**\n   > {gloss}\n   🔗 {url}")

                full = "\n".join(response)
                if len(full) > _DISCORD_LIMIT:
                    full = full[: _DISCORD_LIMIT - 1] + "…"
                await message.channel.send(full)
            except Exception as e:
                logger.exception("Search failed")
                await message.channel.send(
                    f"⚠️ Search error ({_safe_error(e)}); check logs."
                )

    async def process_image_attachment(self, message: discord.Message, attachment: discord.Attachment, parsed: ParsedMessage):
        placeholder = await message.channel.send(f"👁️ Analyzing image `{attachment.filename}`...")
        try:
            image_bytes = await attachment.read()
            result = await describe_image(
                image_bytes=image_bytes,
                mime_type=attachment.content_type or "image/jpeg",
                api_key=self.config["gemini"]["api_key"],
                model=self.config["gemini"].get("model", "gemini-1.5-flash"),
                system_prompt=self.config["gemini"].get("image_system_prompt", ""),
            )
            alt = _cap(result["alt_text"] or "", 500) or None
            transcript = _cap(result["transcription"] or "", 900) or None
            lines = [f"### 🖼️ Image Description: {_cap(attachment.filename, 80)}"]
            if alt:
                lines.append(f"\n**Alt Text:** {alt}")
            if transcript:
                lines.append(f"\n**Transcription:**\n```\n{transcript}\n```")
            content = "\n".join(lines)
            if len(content) > _DISCORD_LIMIT:
                content = content[: _DISCORD_LIMIT - 1] + "…"
            await placeholder.edit(content=content)
        except Exception as e:
            logger.exception("Image attachment failed")
            await placeholder.edit(
                content=f"⚠️ Error analyzing image ({_safe_error(e)})."
            )

    async def process_image_url(self, message: discord.Message, url: str, parsed: ParsedMessage):
        placeholder = await message.channel.send(f"👁️ Analyzing image from URL...")
        try:
            # Cap image fetches at 20 MiB — a little higher than the HTML cap
            # but still bounded so a malicious server can't exhaust memory.
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                try:
                    resp, image_bytes, _ = await safe_get(
                        session, url, max_bytes=20 * 1024 * 1024
                    )
                except UnsafeURLError as e:
                    logger.warning(f"Blocked image fetch {url!r}: {e}")
                    await placeholder.edit(
                        content="⚠️ Image URL blocked (non-public host)."
                    )
                    return
                except ResponseTooLarge:
                    await placeholder.edit(content="⚠️ Image too large to fetch.")
                    return

                if resp.status != 200:
                    await placeholder.edit(content=f"⚠️ Failed to fetch image: HTTP {resp.status}")
                    return
                mime_type = resp.headers.get("Content-Type", "image/jpeg")

            result = await describe_image(
                image_bytes=image_bytes,
                mime_type=mime_type,
                api_key=self.config["gemini"]["api_key"],
                model=self.config["gemini"].get("model", "gemini-1.5-flash"),
                system_prompt=self.config["gemini"].get("image_system_prompt", ""),
            )
            alt = _cap(result["alt_text"] or "", 500) or None
            transcript = _cap(result["transcription"] or "", 900) or None
            lines = [f"### 🖼️ Image Description", f"🔗 {url}"]
            if alt:
                lines.append(f"\n**Alt Text:** {alt}")
            if transcript:
                lines.append(f"\n**Transcription:**\n```\n{transcript}\n```")
            content = "\n".join(lines)
            if len(content) > _DISCORD_LIMIT:
                content = content[: _DISCORD_LIMIT - 1] + "…"
            await placeholder.edit(content=content)
        except Exception as e:
            logger.exception("Image URL failed")
            await placeholder.edit(
                content=f"⚠️ Error analyzing image ({_safe_error(e)})."
            )

    async def process_link(self, message: discord.Message, url: str, parsed: ParsedMessage, domain: str = ""):
        # Pre-flight dedupe: bail before fetch / LLM / archive work. Reacting
        # with 📎 is a quiet signal so the channel doesn't fill with "already
        # archived" noise when people re-share popular links.
        existing = await self.db.find_existing_link(url)
        if existing:
            logger.info(
                f"Dedupe hit: {url} already archived as #{existing['id']}"
            )
            try:
                await message.add_reaction("📎")
            except discord.HTTPException:
                pass
            return

        errors = []
        placeholder = await message.channel.send(f"📚 Archiving `{url}`...")

        try:
            # 1. Fetch, Archive, and Snapshot (Concurrent)
            page_task = asyncio.create_task(fetch_page(url))
            transcript_task = None
            if domain in YOUTUBE_DOMAINS:
                transcript_task = asyncio.create_task(fetch_transcript(url))

            archive_task = None
            if not parsed.no_archive:
                archive_task = asyncio.create_task(
                    archive_url(url, self.config["archive"]["services"], self.config["archive"]["timeout"])
                )
            
            snapshot_task = None
            if self.config["archive"].get("snapshots", {}).get("enabled"):
                snapshot_task = asyncio.create_task(capture_snapshot(url))

            page = await page_task
            transcript = await transcript_task if transcript_task else None
            
            if not page.ok and not transcript:
                errors.append(f"Fetch: {page.error or 'Could not fetch page or transcript'}")

            # 2. AI Logic
            classification: Optional[ClassificationResult] = None
            summary: Optional[SummaryResult] = None
            embedding: Optional[List[float]] = None
            
            ai_input_text = transcript or (page.text if page.ok else "")
            
            if ai_input_text and len(ai_input_text) > 50:
                api_key = self.config["gemini"]["api_key"]

                classification = await classify_and_tag(
                    text=ai_input_text,
                    tag_pool=await self.db.get_tag_pool(),
                    api_key=api_key,
                    model=self.config["gemini"].get("model", "gemini-1.5-flash"),
                    system_prompt=self.config["gemini"].get("tag_system_prompt", ""),
                )
                
                if transcript and classification["genre"] == "Unknown":
                    classification["genre"] = "Video (Transcript)"

                genre = classification.get("genre", "Unknown").lower()
                if any(x in genre for x in ["login", "wall", "auth", "error", "restricted"]):
                    if snapshot_task: snapshot_task.cancel()
                    await placeholder.edit(content=f"🚫 **Access Restricted:** This page appears to be login-locked or private.\n🔗 {url}")
                    return

                if not parsed.effective_no_summary:
                    summary = await generate_summary(
                        text=ai_input_text,
                        title=page.title if page.ok else None,
                        url=url,
                        genre=classification["genre"],
                        metadata=classification["metadata"],
                        api_key=api_key,
                        model=self.config["gemini"].get("model", "gemini-1.5-flash"),
                        system_prompt=self.config["gemini"].get("summary_system_prompt", ""),
                    )

                # Pass 3: Embedding for semantic search (Include message content for context)
                if summary and summary.get("summary"):
                    embedding_text = (
                        f"Title: {page.title or ''}\n"
                        f"Gloss: {summary['gloss'] or ''}\n"
                        f"Summary: {summary['summary']}\n"
                        f"Context: {message.content}"
                    )
                    embedding = await generate_embedding(embedding_text, api_key)

            # 3. Final wait
            archive_result = None
            if archive_task: archive_result = await archive_task
            
            snapshot_result = None
            if snapshot_task:
                try: snapshot_result = await snapshot_task
                except asyncio.CancelledError: pass

            # 4. Final Result
            # Curated tags come from the known pool; proposed tags are LLM
            # inventions that go into tag_proposals for counting.  Both are
            # shown in the Discord message so the channel is informative, but
            # only curated tags are linked in the DB.
            curated_tags  = classification["tags"]     if classification else []
            proposed_tags = classification["new_tags"] if classification else []
            all_tags = curated_tags + proposed_tags
            final_msg = format_archive_message(
                url=url,
                title=page.title if page.ok else (f"Video: {domain}" if domain in VIDEO_DOMAINS else None),
                gloss=summary["gloss"] if summary else None,
                summary=summary["summary"] if summary else None,
                tags=all_tags if all_tags else None,
                archive_url=archive_result.url if archive_result and archive_result.ok else None,
                archive_service=archive_result.service if archive_result and archive_result.ok else None,
                commentary=parsed.commentary or None,
                author_name=message.author.display_name,
                errors=errors if errors else None,
                privacy_mode=parsed.privacy,
            )
            if snapshot_result and snapshot_result.ok:
                final_msg += f"\n📸 [PDF Snapshot saved to library]"

            await placeholder.edit(content=final_msg)

            if snapshot_result and snapshot_result.ok:
                try:
                    file = discord.File(snapshot_result.pdf_path, filename=Path(snapshot_result.pdf_path).name)
                    await message.channel.send(f"📄 Full-page PDF for `{url}`:", file=file)
                except Exception as e:
                    logger.warning(
                        f"Failed to upload PDF to Discord: {type(e).__name__}: {e}"
                    )

            # 5. DB Save
            await self.db.save_link(
                url=url,
                title=page.title if page.ok else None,
                genre=classification["genre"] if classification else None,
                gloss=summary["gloss"] if summary else None,
                summary=summary["summary"] if summary else None,
                commentary=parsed.commentary or None,
                original_message=message.content,
                archive_url=archive_result.url if archive_result and archive_result.ok else None,
                archive_service=archive_result.service if archive_result and archive_result.ok else None,
                snapshot_path=snapshot_result.pdf_path if snapshot_result and snapshot_result.ok else None,
                embedding=embedding,
                guild_id=message.guild.id if message.guild else None,
                channel_id=message.channel.id,
                message_id=message.id,
                author_id=message.author.id,
                bot_message_id=placeholder.id,
                tags=curated_tags if curated_tags else None,
                proposed_tags=proposed_tags if proposed_tags else None,
                summary_skipped=parsed.effective_no_summary,
                tags_skipped=parsed.effective_no_tags,
                archive_skipped=parsed.no_archive,
                privacy_mode=parsed.privacy,
            )

        except Exception as e:
            logger.exception(f"Error processing {url}")
            try:
                await placeholder.edit(
                    content=f"📚 Archived: {url}\n-# ⚠️ Processing error ({_safe_error(e)}); check logs."
                )
            except Exception:
                logger.exception("Failed to edit placeholder message after error")


def main():
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    config = load_config()
    bot = ArchivistBot(config)
    bot.run(config["discord"]["token"], log_handler=None)

if __name__ == "__main__":
    main()
