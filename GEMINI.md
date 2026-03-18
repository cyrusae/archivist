# GEMINI.md - Project Archivist

This file provides foundational mandates and context for AI agents working on the Archivist project.

## Project Vision

Archivist is a "Digital Librarian" Discord bot. It archives links, snapshots pages as PDFs (uploaded to Discord), performs multimodal analysis (OCR/Vision) on images, and uses a two-pass AI flow to classify and summarize content.

## Completed Features

- [x] **YouTube Intelligence:** Automatic transcript extraction using `youtube-transcript-api` and Gemini summarization.
- [x] **Semantic Search (pgvector):** `!search <query>` command (restricted to owner) using Gemini embeddings that include the original message context.
- [x] **Context Persistence:** Original Discord message content is saved in the database and indexed for search.
- [x] **Concurrent Link Processing:** Handles multiple links in a single message by spawning parallel processing pipelines.
- [x] **Daily Library Digest:** Scheduled Markdown digest DM'd to the owner, tracking source user and channel.
- [x] **Multimodal Vision:** OCR and Alt-text generation for images (links or attachments) with `-alt` flag support.
- [x] **PDF Snapshots:** Full-page captures using Playwright, uploaded directly to Discord.
- [x] **Hierarchical Config:** Global < Server < Channel < Role override logic.
- [x] **Librarian Logic:** AO3 full-work augmentation and automated Login-Wall detection.

## Core Technical Stack

- **Language:** Python 3.12+ (managed by `uv`)
- **Discord Library:** `discord.py` (requires Message Content and Members intents)
- **AI Engine:** Google Gemini (1.5 Flash for summary/vision, 004 for embeddings)
- **Database:** PostgreSQL with `pgvector` extension
- **Archival:** `archive.is` + `Wayback` + `Playwright`
- **Extraction:** `readability-lxml` + `strip-tags`

## Architectural Mandates

1. **Multimodal Awareness:** Use `describe_image` in `ai.py` for all visual content.
2. **Two-Pass AI Intelligence:** Always classify genre before summarizing to tailor the prompt.
3. **Semantic Indexing:** Generate and store embeddings for all successful summaries at `save_link` time.
4. **Archival Strategy:** Run URL archival and PDF snapshots concurrently. Upload PDFs to Discord immediately.
5. **Clean Extraction:** Minify content before LLM submission.

## File Map

- `bot.py`: Main orchestrator, background tasks, and search command.
- `ai.py`: Gemini integrations (Classification, Summary, Vision, Embeddings).
- `snapshot.py`: Playwright PDF capture logic.
- `youtube.py`: YouTube transcript extraction.
- `fetcher.py`: Web fetching and content cleaning.
- `archiver.py`: Web archive service wrappers.
- `db.py`: PostgreSQL schema and vector search methods.
- `parser.py`: Message parsing and link exclusion.
- `formatter.py`: Discord/Obsidian markdown formatting.

## Future Roadmap

- [ ] **Garage (S3-compatible) Integration:** Move PDF snapshots from local disk to Garage.
- [ ] **Improved Video Metadata:** Pull YouTube titles/descriptions via API if transcript fails.
- [ ] **Web Dashboard:** Simple internal UI for browsing the library.
- [ ] Improve UX for conditional configurations of whether to default to summary/track specific channels/etc. (simple web UI?)
- [ ] Reorganize repo (separate .py files and docs)
- [ ] More detailed documentation

- [ ] Human review of README (remind human to do this after major updates)
