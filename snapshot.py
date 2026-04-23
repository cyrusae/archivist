"""Snapshot service: PDF snapshots using Playwright.

SSRF note: we validate the *initial* URL against `net.validate_public_url`
before launching the browser, but Playwright performs its own DNS resolution
and follows redirects in-browser — a redirect to a private host would still be
fetched by Chromium. Deeper hardening (request interception via `page.route`
with a per-request validator) is tracked separately in `FIX_PLAN.md`.
"""

import logging
import asyncio
import hashlib
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

from playwright.async_api import async_playwright

from net import UnsafeURLError, validate_public_url

logger = logging.getLogger("archivist.snapshot")

@dataclass
class SnapshotResult:
    pdf_path: Optional[str] = None
    error: Optional[str] = None
    ok: bool = False

async def capture_snapshot(url: str, output_dir: str = "snapshots") -> SnapshotResult:
    """
    Capture a PDF snapshot of a URL using Playwright.
    """
    try:
        await validate_public_url(url)
    except UnsafeURLError as e:
        logger.warning(f"Blocked snapshot of {url!r}: {e}")
        return SnapshotResult(error="URL blocked (non-public host)", ok=False)

    out_path = Path(output_dir)
    out_path.mkdir(exist_ok=True, parents=True)

    url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
    pdf_file = out_path / f"snap_{url_hash}.pdf"

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            )
            page = await context.new_page()
            
            # Wait until network is idle
            await page.goto(url, wait_until="networkidle", timeout=60000)
            
            # Generate PDF
            await page.pdf(
                path=str(pdf_file),
                format="A4",
                print_background=True,
                margin={"top": "1cm", "right": "1cm", "bottom": "1cm", "left": "1cm"}
            )
            
            await browser.close()
            logger.info(f"PDF snapshot saved: {pdf_file}")
            return SnapshotResult(pdf_path=str(pdf_file), ok=True)

    except Exception as e:
        logger.warning(f"Snapshot failed for {url}: {e}")
        return SnapshotResult(error=str(e), ok=False)
