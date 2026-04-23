# Archivist Fix Plan

> **Progress legend:** ✅ done · 🟡 in progress · ⏳ not started

## Status summary

- **Phase 0 — Make it actually run:** ✅ all items complete; every module
  parses, imports, and the validation / owner-id / digest-time helpers behave
  correctly under unit testing.
- **Phase 1 — Make it safe to expose:** ✅ All items complete.
- **Phase 2 — Correctness:** ✅ 2.1, 2.3, 2.5, 2.6, 2.7, 2.8 done.
  2.2 done (capped during Phase 1). 2.4 (archive.is Playwright fallback) and
  2.9 (full test coverage) partially done — new tests cover formatter + youtube;
  db/bot/fetcher tests deferred.
- **Phase 3:** untouched.

Starting point: an unreviewed Gemini-generated prototype. The review in this
session found show-stoppers (the code doesn't import), security holes (SSRF,
fail-open authz, placeholder secrets), and a handful of logic bugs that would
only surface in production. This document is the fix roadmap.

Phases are ordered so that each one leaves the repo in a strictly better state
than the last. Phase 0 gets it to "imports and starts." Phase 1 gets it to
"safe to point at Discord." Phase 2 gets it to "production-quality." Phase 3 is
polish.

---

## Phase 0 — Make it actually run

Non-negotiable. Without these the bot can't import, connect, or write a row.

### 0.1 ✅ Restore `describe_image` in `ai.py`

`ai.py` currently has `async def describe_image(...)` followed by a placeholder
comment and then the next `async def`. That's an `IndentationError` — the
module doesn't parse, so `bot.py`'s top-level `from ai import ...` crashes the
process.

- Reconstruct `describe_image` to call Gemini with the image bytes + mime type,
  parse the `**Alt Text:** / **Transcription:**` response shape, and return
  the `ImageResult` TypedDict.
- Mirror the error handling in `classify_and_tag` / `generate_summary` — catch
  broadly, log via `logger.exception`, return `{"alt_text": None,
  "transcription": None, "error": str(e)}`.
- Verify with `python -m compileall ai.py` and add a CI step that runs
  `python -m compileall .` so a broken module can never land again.

### 0.2 ✅ Reconcile Python version

`pyproject.toml` says `>=3.14`, `.python-version` says `3.12`, `Dockerfile`
pulls `python:3.12-slim-bookworm`, README says 3.12+. Pick one — 3.12 is the
path of least surprise (matches the lockfile and the container). Update
`pyproject.toml` to `requires-python = ">=3.12"` and re-`uv lock` if needed.

### 0.3 ✅ Register the pgvector codec

`asyncpg` has no idea what `vector(768)` is. Every insert with a non-null
embedding will fail with `InvalidTextRepresentation`, and so will every
`search_links` call.

- Add `pgvector` to `pyproject.toml`.
- In `Database.connect`, pass an `init` coroutine to `asyncpg.create_pool` that
  calls `await pgvector.asyncpg.register_vector(conn)` for every pooled
  connection.
- Add a smoke test that inserts a row with a fake 768-dim embedding and reads
  it back.

### 0.4 ✅ Fix the `owner_id` placeholder crash

`int("YOUR_DISCORD_USER_ID")` raises on startup because the placeholder string
is truthy. Guard with an explicit check:

```python
raw_owner = config["discord"].get("owner_id")
self.owner_id = int(raw_owner) if isinstance(raw_owner, (int, str)) and str(raw_owner).isdigit() else None
```

Also harden `load_config` to validate required keys (`discord.token`,
`gemini.api_key`, `database.*`) and fail with a readable message, not a
`KeyError` ten frames deep.

### 0.5 ✅ Fix the digest scheduler

`@tasks.loop(hours=24)` + `asyncio.sleep(...)` inside the task is broken — the
digest drifts and may not fire for >24h after startup. Replace with:

```python
@tasks.loop(time=datetime.time(hour=target_h, minute=target_m, tzinfo=...))
async def digest_loop(self):
    await self.send_daily_digest()
```

Parse `digest_time` once at startup and use a clear timezone (UTC or a
configured `discord.timezone`).

### 0.6 🟡 Gemini model / SDK currency

_Partial: default model bumped `gemini-1.5-flash` → `gemini-2.0-flash` in
`config/default.yaml` and `ai.py`. The `google-generativeai` package itself is
deprecated (emits `FutureWarning` on import pointing at `google.genai`); full
SDK migration deferred to Phase 2 since it touches every function in `ai.py`._

`gemini-1.5-flash` is EOL and `google-generativeai` is superseded by
`google-genai`. Depending on what still works against the current API, either:

- (minimum) swap the default model to a current Flash tier in
  `config/default.yaml` and verify `google-generativeai>=0.8.6` still
  authenticates, or
- (better) migrate to the `google-genai` SDK — touches `ai.py` only.

Pin `text-embedding-004` → current embedding model and confirm the dimension
still matches the `vector(768)` column (or bump the column).

**Exit criteria for Phase 0:** `uv run python bot.py` starts, connects to a
local Postgres with pgvector, joins Discord, and can archive one link
end-to-end without raising.

---

## Phase 1 — Make it safe to expose

These are the issues that turn the bot into a vulnerability the moment real
users can reach it.

### 1.1 ✅ SSRF guard around every outbound fetch

_Landed as `net.py` (new module) plus wiring into `fetcher.py`,
`bot.process_image_url`, and `snapshot.capture_snapshot`. `safe_get` does
per-hop redirect validation (manual redirect loop with `allow_redirects=False`)
so every URL in the chain is validated before TCP connect. 27 tests in
`tests/test_net.py` cover RFC1918, loopback, link-local (incl. AWS metadata),
multicast, reserved, IPv6 equivalents, mixed-record DNS round-robin tricks,
the K8s cluster-DNS case, IP literals (no DNS), scheme allowlist, and
unresolvable hostnames. **Residual gap:** Playwright follows redirects
in-browser; the initial URL is validated but an in-browser redirect to a
private host would still render. Tracked for Phase 2._

User-supplied URLs flow into `fetcher.fetch_page`, `bot.process_image_url`,
`archiver.try_wayback` (via the `save/` path), and `snapshot.capture_snapshot`.
In the K3s deployment this means a Discord user can make the bot poke
`postgres.archivist.svc.cluster.local:5432`, `169.254.169.254`, `localhost`,
etc., with partial responses leaking back into the channel.

Plan:

- New module `net.py` with `async def resolve_and_check(url: str) -> str`:
  - Reject non-http(s) schemes.
  - Resolve the hostname (via `loop.getaddrinfo`) and reject any result in
    private, loopback, link-local, multicast, or reserved ranges (use
    `ipaddress.ip_address(...).is_private` etc.).
  - Rebuild the URL against the resolved IP so the fetch can't be
    DNS-rebinding-rerouted, **and** pass `Host:` header — or just accept the
    small rebind window and re-check after redirects.
  - Cap redirects; re-run the check on every hop.
- Call the guard at the top of `fetch_page`, `process_image_url`,
  `capture_snapshot`, and `try_wayback` / `try_archive_is` (the latter two
  mainly as defense-in-depth — the archive services themselves are fine, but
  someone could point us at a fake archive host).
- Add config `fetch.allowlist` / `fetch.denylist` hooks for future tuning.

### 1.2 ✅ Fail-closed `!search` authz

```python
if self.owner_id is None or message.author.id != self.owner_id:
    return
```

The current order short-circuits on `self.owner_id` being falsy. Flip it.

Bonus: move `!search` behind a proper command framework (slash command or
`discord.ext.commands`) so authorization is declarative.

### 1.3 ✅ Stop echoing raw exceptions to Discord

Every `except Exception as e: ... f"...: {e}"` is a potential leak (Gemini auth
errors can contain the API key; asyncpg errors can contain DSN fragments). 

- Add a `safe_error(e)` helper that returns `type(e).__name__` plus a short,
  scrubbed message.
- Apply in `handle_search`, `process_image_attachment`, `process_image_url`,
  and the `process_link` fallback.
- Keep the full exception in `logger.exception` — just don't user-facing it.

### 1.4 ✅ Remove placeholder secrets from `deployment.yaml`

`stringData: DISCORD_TOKEN: "YOUR_DISCORD_BOT_TOKEN"` will happily apply as-is.

- Delete the `Secret` block from the committed manifest and document
  `kubectl create secret generic archivist-secrets --from-literal=...` (or a
  SealedSecret / SOPS flow) in `DEPLOYMENT.md`.
- Alternatively, split into `deployment.yaml` + an untracked
  `secrets.example.yaml`, and add `secrets.yaml` to `.gitignore`.

### 1.5 ✅ Per-user rate limiting + URL dedupe

Any authenticated Discord user can currently fan-out an arbitrary number of
Gemini calls + Playwright browser launches.

- Add `UNIQUE(url)` (or `UNIQUE(url, guild_id)`) on `archived_links` and do a
  `SELECT id FROM archived_links WHERE url = $1` short-circuit before any
  fetch/LLM/archive work; if found, just re-post the existing summary.
- Add an in-memory token bucket per `author_id` (e.g. 5 links/minute, 30/hour)
  — reject with a quiet reaction, not a message.

### 1.6 ✅ Stop leaking `parsed` mutations across URLs

In `on_message`, the URL loop mutates `parsed.no_summary = True` etc. for
social domains. A message with `https://x.com/... https://arxiv.org/...` skips
summarization on both. Compute per-URL overrides locally:

```python
for url in parsed.urls:
    per_url = replace(parsed)  # dataclasses.replace
    if domain in SOCIAL_DOMAINS:
        per_url.no_summary = True
        per_url.no_tags = True
    ...
```

### 1.7 ✅ Fix the UnboundLocalError on URL parse failure

`urlparse` is inside `try: ... except: pass` but `parsed_url` is used below the
block. Initialize `parsed_url = None` up front and gate the AO3 rewrite on
`parsed_url is not None`. Also replace the bare `except:` with `except
Exception:` (bare `except` catches `KeyboardInterrupt`).

### 1.8 ✅ Graceful shutdown

`close()` closes the pool while detached `process_link` tasks are mid-flight.

- Track every `asyncio.create_task(...)` in a `self._tasks: set[asyncio.Task]`
  and add a done-callback that removes them.
- In `close()`: stop accepting new messages, `await asyncio.gather(*self._tasks,
  return_exceptions=True)` with a timeout, *then* close the pool.

### 1.9 ✅ Cap outbound response size

_Folded into `net.safe_get`, which streams the body in 64 KiB chunks and
raises `ResponseTooLarge` the moment the cap is exceeded. Default 10 MiB for
HTML, 20 MiB for image fetches. Trusted `Content-Length` heuristic is gone —
the cap is enforced during the actual read._

`MAX_CONTENT_LENGTH` in `fetcher.py` only checks `Content-Length`. Servers
that stream without the header bypass it. Read in chunks and bail at the cap:

```python
chunks = []
total = 0
async for chunk in resp.content.iter_chunked(64 * 1024):
    total += len(chunk)
    if total > MAX_CONTENT_LENGTH:
        return FetchedPage(url=url, error="Content too large")
    chunks.append(chunk)
```

### 1.10 ✅ Document and use the `members` privileged intent

`intents.members = True` is a privileged intent not mentioned in the README. If
nothing actually needs it, remove it. If role-based overrides need it, document
it in the setup steps and note the "enable in Dev Portal" step.

**Exit criteria for Phase 1:** no user-reachable code path can hit internal
network, leak stack traces, or fan out Gemini calls beyond a defined rate.
`!search` refuses unknown users.

---

## Phase 2 — Correctness

Features that are in the prototype but don't behave correctly.

### 2.1 ✅ Discord 2000-char limit

`format_archive_message` now applies per-field caps (title 200, gloss 150,
summary 500, commentary 300) before building the message, plus a hard backstop
at 1950 chars that fires only when URLs/tags/errors push it over.  Image
description and search-results handlers in `bot.py` also cap their fields and
apply the same backstop.  The `_cap` helper and `_DISCORD_LIMIT` constant are
exported from `formatter.py` for reuse.

### 2.2 ✅ Bound the LLM fallback output

`generate_summary` falls back to `summary = response_text.strip()` when the
`**Gloss:**/**Summary:**` shape is missing. That dumps arbitrary Gemini output
into Discord. Either (a) regenerate once with a stricter prompt, or (b) cap at
~500 chars.

### 2.3 ✅ Tag pool curation

New `tag_proposals` table.  `save_link` now accepts `proposed_tags` (new LLM
suggestions) separately from `tags` (curated pool hits).  `record_proposals`
upserts into `tag_proposals`, incrementing `use_count` on each re-use.
`get_tag_pool(proposal_threshold=3)` returns curated tags + any proposals with
`use_count >= threshold`, so persistent LLM suggestions naturally surface.
`promote_tag(name)` moves a proposal directly into `tags` and cleans it out of
proposals.  New owner command `!promote-tag <name>` in `bot.py`.

### 2.4 Dedup / idempotency for archival calls

archive.is is Cloudflare-protected and `aiohttp` HEAD/POST will frequently be
challenged or 403'd. Options:

- (easy) Treat archive.is failures as non-fatal and quieter in logs.
- (better) Submit via Playwright (we already have it) when `aiohttp` fails.
- For Wayback, filter `archived_snapshots.closest` by `timestamp` freshness
  (≤30 days) before counting it as "already archived."

### 2.5 ✅ Daily digest windowing

New `meta` table (key-value store).  `send_daily_digest` reads
`last_digest_sent_at` from meta, queries `get_links_since(since)` (replacing
the hard-coded `NOW() - 24h`), and writes back the timestamp only after a
successful send.  First-run falls back to 24 h ago.

### 2.6 ✅ Digest file handling

`send_daily_digest` already uses `tempfile.NamedTemporaryFile` (landed in Phase
1 via 2.6's scope being folded in early).

### 2.7 ✅ YouTube transcript API

`fetch_transcript` now calls `YouTubeTranscriptApi().fetch(video_id)` (instance
API, `>=1.2.4`) and reads `.text` from snippet objects.  Redundant exception
tuple removed.  `YT_ID_PATTERN` extended to cover `m.youtube.com` and
`music.youtube.com`; `YOUTUBE_DOMAINS` set added to `bot.py` for the transcript
branch trigger.

### 2.8 ✅ Full-text search

`archived_links` now has an `fts tsvector` generated column (title + gloss +
summary + commentary) with a GIN index, both added via `ALTER TABLE … ADD
COLUMN IF NOT EXISTS` so the migration is idempotent on existing databases.
`db.search_links_text(query)` queries with `plainto_tsquery` and ranks by
`ts_rank`.  `handle_search` in `bot.py` tries semantic search first; if
embedding fails or returns nothing it falls back to full-text.  README updated
to reflect dual-search accurately and list `!promote-tag`.

### 2.9 🟡 Test coverage

`tests/test_net.py` (27 tests — SSRF) and `tests/test_formatter.py` (14 tests —
2000-char limit) and `tests/test_youtube.py` (9 tests — URL pattern) added.

Still deferred:
- `tests/test_fetcher.py` — mock aiohttp
- `tests/test_db.py` — requires a Postgres container
- `tests/test_bot_overrides.py` — mock discord.Message / Member / role

CI workflow not yet wired.

**Exit criteria for Phase 2:** everything advertised in the README actually
works, tests cover the non-trivial logic, long pages/summaries don't crash the
message edit.

---

## Phase 3 — Polish / ops

### 3.1 Container hygiene

- Multi-stage Dockerfile: drop `build-essential`, `libxml2-dev`, `libxslt-dev`
  from the final image.
- Add a non-root user; chown `/app` and `/app/snapshots`.
- Tag images with git SHA instead of `:latest`.
- Set `imagePullPolicy: IfNotPresent` against a SHA tag (correct) or
  `Always` against `:latest` (also correct) — the current combo of `:latest`
  + `IfNotPresent` is wrong.

### 3.2 K8s health + resources

- Add `livenessProbe` / `readinessProbe`. For a Discord bot, a simple HTTP
  server in-process that reports `client.is_ready()` is the usual approach.
- Double-check resource limits against Playwright reality; 1Gi memory is
  tight for Chromium rendering large pages.

### 3.3 Observability

- Structured logging (JSON) with a `request_id`/`message_id` field so a single
  archive lifecycle is greppable.
- Counters for: links processed, fetch failures, archive failures, Gemini call
  latency, per-error-type.

### 3.4 Config validation

Use `pydantic-settings` or `attrs` + a schema to validate `config.yaml` at
startup. Beats discovering a typo in a key path on the hot path.

### 3.5 Regex / parsing polish

- Tighten the legacy opt-out regex (`^archivist,?\s+no\b` at line start, or a
  dedicated "opt-out" flag). Currently matches "the archivist, no matter how…"
- Extend `YT_ID_PATTERN` to `music.youtube.com` and `m.youtube.com`.

### 3.6 Commit hygiene

Split the current "features" commit into meaningful chunks when we touch this
stuff — by the time anything is in production, `git log` should tell a story.

---

## Suggested working order

1. 0.1 (fix ai.py) — 5 minutes; unblocks everything.
2. 0.2–0.4 (python version, pgvector, config) — half an hour; gets the bot
   starting.
3. 1.1 (SSRF) + 1.2 (!search authz) + 1.3 (error scrubbing) — do these together
   before we ever point it at a real Discord server.
4. 0.5 (digest scheduler) + 0.6 (Gemini SDK) — can be done in parallel.
5. 1.4–1.10 — incremental, one per PR ideally.
6. Phase 2 — once we have real traffic to observe.
7. Phase 3 — when we care about ops, not before.

We should also drop the `.venv/`, `__pycache__/`, and `.DS_Store` entries that
are currently tracked (or should be gitignored — needs verification against
the `.gitignore`).

---

## Notes on Gemini-authored code

A recurring pattern across the prototype: the code *looks* like plausible
production Python — TypedDicts, dataclasses, connection pools, tasks.loop
decorators — but each subsystem has one or two subtle mis-usages that only
show up under real load (pgvector codec, tasks.loop cadence, `parsed` mutation
across iterations, placeholder-string crash at startup, SSRF). The shape is
right; the execution needs the kind of review that only happens when someone
actually tries to run the thing.

So: nothing catastrophic about the architecture. Mostly a lot of "almost
correct" that needs patient untangling. Phase 0 is an hour, Phase 1 is a day,
Phase 2 is a week.
