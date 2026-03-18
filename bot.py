"""
Archivist - A Discord bot for intelligent link archiving.
"""

import asyncio
import logging
import os
import re
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Optional, List, Set, Any, Dict
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

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
from formatter import format_archive_message
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
        self.owner_id: Optional[int] = int(config["discord"].get("owner_id")) if config["discord"].get("owner_id") else None

    async def setup_hook(self):
        await self.db.connect()
        await self.db.seed_tags(self.config.get("seed_tags", []))
        
        # Start background task if owner is set
        if self.owner_id:
            self.digest_loop.start()
            
        logger.info("Archivist is ready.")

    async def close(self):
        await self.db.close()
        await super().close()

    @tasks.loop(hours=24)
    async def digest_loop(self):
        """Send a daily digest of all archived links to the owner."""
        digest_time_str = self.config["discord"].get("digest_time", "08:00")
        target_h, target_m = map(int, digest_time_str.split(":"))
        
        now = datetime.now()
        target_time = now.replace(hour=target_h, minute=target_m, second=0, microsecond=0)
        
        if now > target_time:
            target_time += timedelta(days=1)
            
        wait_seconds = (target_time - now).total_seconds()
        logger.info(f"Daily digest scheduled for {target_time} (waiting {wait_seconds:.0f}s)")
        await asyncio.sleep(wait_seconds)
        
        try:
            await self.send_daily_digest()
        except Exception as e:
            logger.exception("Failed to send daily digest")

    async def send_daily_digest(self):
        """Generate and send the digest."""
        if not self.owner_id:
            return
            
        links = await self.db.get_daily_links()
        if not links:
            logger.info("No links found for daily digest.")
            return
            
        owner = await self.fetch_user(self.owner_id)
        if not owner:
            logger.error(f"Could not find owner with ID {self.owner_id}")
            return
            
        date_str = datetime.now().strftime("%Y-%m-%d")
        filename = f"archivist_digest_{date_str}.md"
        
        content = [f"# 📚 Archivist Daily Digest - {date_str}\n"]
        
        for link in links:
            title = link.get("title") or "Untitled"
            url = link.get("url")
            author_id = link.get("author_id")
            channel_id = link.get("channel_id")
            gloss = link.get("gloss") or ""
            
            author_tag = f"<@{author_id}>"
            channel_tag = f"<#{channel_id}>"
            
            content.append(f"## {title}")
            content.append(f"- **URL:** {url}")
            content.append(f"- **Shared by:** {author_tag} in {channel_tag}")
            if gloss:
                content.append(f"- **Gloss:** {gloss}")
            content.append("")
            
        full_text = "\n".join(content)
        
        with open(filename, "w") as f:
            f.write(full_text)
            
        try:
            await owner.send(
                f"📊 Good morning! Here is your Archivist digest for {date_str}.",
                file=discord.File(filename)
            )
        finally:
            if os.path.exists(filename):
                os.remove(filename)
        
        logger.info(f"Daily digest sent to {owner.name}")

    def get_overrides(self, message: discord.Message) -> Dict[str, bool]:
        """Apply hierarchical overrides: Global < Server < Channel < Role."""
        res = self.config.get("defaults", {}).copy()
        ovr = self.config.get("overrides", {})

        if message.guild:
            sid = str(message.guild.id)
            if sid in ovr.get("servers", {}):
                res.update(ovr["servers"][sid])
                if isinstance(message.author, discord.Member):
                    for role in message.author.roles:
                        rid = str(role.id)
                        if rid in ovr["servers"][sid].get("roles", {}):
                            res.update(ovr["servers"][sid]["roles"][rid])

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

        # --- PREFIX COMMANDS ---
        if message.content.startswith("!search "):
            # Restrict search to owner
            if self.owner_id and message.author.id != self.owner_id:
                # Silently ignore or send a subtle message?
                # User asked to restrict it, so we'll just return.
                return
                
            query = message.content[8:].strip()
            if query:
                await self.handle_search(message, query)
            return
        # -----------------------

        if not self.should_watch(message.channel.id):
            return

        parsed = parse_message(message.content)
        
        # Apply Overrides
        overrides = self.get_overrides(message)
        if not overrides.get("summary", True): parsed.no_summary = True
        if not overrides.get("tags", True): parsed.no_tags = True
        if not overrides.get("archive", True): parsed.no_archive = True

        if not parsed.should_process and not message.attachments:
            return

        # Handle explicit attachments
        for attachment in message.attachments:
            if any(attachment.filename.lower().endswith(ext) for ext in IMAGE_EXTENSIONS):
                asyncio.create_task(
                    self.process_image_attachment(message, attachment, parsed),
                    name=f"image-{attachment.id}",
                )

        # Handle URLs
        for url in parsed.urls:
            domain = ""
            try:
                parsed_url = urlparse(url)
                domain = parsed_url.netloc.lower()
                if domain.startswith("www."): domain = domain[4:]
            except: pass

            if domain == "archiveofourown.org":
                query = parse_qs(parsed_url.query)
                query["view_adult"] = ["true"]
                query["view_full_work"] = ["true"]
                url = urlunparse(parsed_url._replace(query=urlencode(query, doseq=True)))

            if domain in SOCIAL_DOMAINS:
                parsed.no_summary = True
                parsed.no_tags = True

            if any(url.lower().split('?')[0].endswith(ext) for ext in IMAGE_EXTENSIONS):
                asyncio.create_task(
                    self.process_image_url(message, url, parsed),
                    name=f"image-url-{url[:50]}",
                )
            else:
                asyncio.create_task(
                    self.process_link(message, url, parsed, domain),
                    name=f"archive-{url[:50]}",
                )

    async def handle_search(self, message: discord.Message, query: str):
        """Handle semantic search query."""
        async with message.channel.typing():
            try:
                api_key = self.config["gemini"]["api_key"]
                embedding = await generate_embedding(query, api_key)
                
                if not embedding:
                    await message.channel.send("⚠️ Failed to generate search embedding.")
                    return
                
                results = await self.db.search_links(embedding, limit=5)
                if not results:
                    await message.channel.send("📂 No relevant matches found in the archive.")
                    return
                
                response = ["### 🔍 Semantic Search Results"]
                for i, res in enumerate(results, 1):
                    title = res.get("title") or "Untitled"
                    gloss = res.get("gloss") or ""
                    url = res.get("url")
                    response.append(f"{i}. **{title}**\n   > {gloss}\n   🔗 {url}")
                
                await message.channel.send("\n".join(response))
            except Exception as e:
                logger.exception("Search failed")
                await message.channel.send(f"⚠️ Search error: {e}")

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
            lines = [f"### 🖼️ Image Description: {attachment.filename}"]
            if result["alt_text"]: lines.append(f"\n**Alt Text:** {result['alt_text']}")
            if result["transcription"]: lines.append(f"\n**Transcription:**\n```\n{result['transcription']}\n```")
            await placeholder.edit(content="\n".join(lines))
        except Exception as e:
            logger.exception("Image attachment failed")
            await placeholder.edit(content=f"⚠️ Error analyzing image: {e}")

    async def process_image_url(self, message: discord.Message, url: str, parsed: ParsedMessage):
        placeholder = await message.channel.send(f"👁️ Analyzing image from URL...")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        await placeholder.edit(content=f"⚠️ Failed to fetch image: HTTP {resp.status}")
                        return
                    image_bytes = await resp.read()
                    mime_type = resp.headers.get("Content-Type", "image/jpeg")

            result = await describe_image(
                image_bytes=image_bytes,
                mime_type=mime_type,
                api_key=self.config["gemini"]["api_key"],
                model=self.config["gemini"].get("model", "gemini-1.5-flash"),
                system_prompt=self.config["gemini"].get("image_system_prompt", ""),
            )
            lines = [f"### 🖼️ Image Description", f"🔗 {url}"]
            if result["alt_text"]: lines.append(f"\n**Alt Text:** {result['alt_text']}")
            if result["transcription"]: lines.append(f"\n**Transcription:**\n```\n{result['transcription']}\n```")
            await placeholder.edit(content="\n".join(lines))
        except Exception as e:
            logger.exception("Image URL failed")
            await placeholder.edit(content=f"⚠️ Error analyzing image: {e}")

    async def process_link(self, message: discord.Message, url: str, parsed: ParsedMessage, domain: str = ""):
        errors = []
        placeholder = await message.channel.send(f"📚 Archiving `{url}`...")

        try:
            # 1. Fetch, Archive, and Snapshot (Concurrent)
            page_task = asyncio.create_task(fetch_page(url))
            transcript_task = None
            if domain in ["youtube.com", "youtu.be"]:
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
            all_tags = (classification["tags"] + classification["new_tags"]) if classification else []
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
                    logger.warning(f"Failed to upload PDF to Discord: {e}")

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
                tags=all_tags if all_tags else None,
                summary_skipped=parsed.effective_no_summary,
                tags_skipped=parsed.effective_no_tags,
                archive_skipped=parsed.no_archive,
                privacy_mode=parsed.privacy,
            )

        except Exception as e:
            logger.exception(f"Error processing {url}")
            try: await placeholder.edit(content=f"📚 Archived: {url}\n-# ⚠️ Processing error: {e}")
            except: pass


def main():
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    config = load_config()
    bot = ArchivistBot(config)
    bot.run(config["discord"]["token"], log_handler=None)

if __name__ == "__main__":
    main()
