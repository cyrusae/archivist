"""Archival service integrations (archive.is, Wayback Machine)."""

import logging
from dataclasses import dataclass
from typing import Optional, List, Callable, Awaitable

import aiohttp

logger = logging.getLogger("archivist.archiver")


@dataclass
class ArchiveResult:
    url: Optional[str] = None
    service: Optional[str] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.url is not None


async def try_archive_is(url: str, timeout: int = 30) -> ArchiveResult:
    """
    Submit a URL to archive.today (archive.is).
    """
    try:
        check_url = f"https://archive.is/newest/{url}"
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as session:
            async with session.head(check_url, allow_redirects=True) as resp:
                if resp.status == 200 and ("archive.is" in str(resp.url) or "archive.today" in str(resp.url)):
                    return ArchiveResult(url=str(resp.url), service="archive.is")

            # If no existing snapshot, try to create one
            async with session.post(
                "https://archive.is/submit/",
                data={"url": url, "anyway": 1},
                allow_redirects=True,
            ) as resp:
                if resp.status == 200:
                    final_url = str(resp.url)
                    if "archive.is" in final_url or "archive.today" in final_url:
                        return ArchiveResult(url=final_url, service="archive.is")

        return ArchiveResult(error="archive.is: no snapshot created")

    except aiohttp.ClientError as e:
        return ArchiveResult(error=f"archive.is: {type(e).__name__}")
    except Exception as e:
        logger.warning(f"archive.is error for {url}: {e}")
        return ArchiveResult(error=f"archive.is: {e}")


async def try_wayback(url: str, timeout: int = 30) -> ArchiveResult:
    """
    Submit a URL to the Wayback Machine's Save Page Now API.
    """
    try:
        api_base = "https://web.archive.org"
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as session:
            # First, check for existing snapshot
            avail_url = f"{api_base}/wayback/available?url={url}"
            async with session.get(avail_url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    snapshot = data.get("archived_snapshots", {}).get("closest")
                    if snapshot and snapshot.get("available"):
                        return ArchiveResult(
                            url=snapshot["url"], service="wayback"
                        )

            # Try to save a new snapshot
            save_url = f"{api_base}/save/{url}"
            async with session.get(save_url, allow_redirects=True) as resp:
                if resp.status == 200:
                    final = str(resp.url)
                    if "web.archive.org" in final:
                        return ArchiveResult(url=final, service="wayback")

        return ArchiveResult(error="wayback: no snapshot available")

    except aiohttp.ClientError as e:
        return ArchiveResult(error=f"wayback: {type(e).__name__}")
    except Exception as e:
        logger.warning(f"Wayback error for {url}: {e}")
        return ArchiveResult(error=f"wayback: {e}")


async def archive_url(url: str, services: List[str], timeout: int = 30) -> ArchiveResult:
    """
    Try archival services in order, return first success.
    """
    service_map: dict[str, Callable[[str, int], Awaitable[ArchiveResult]]] = {
        "archive_is": try_archive_is,
        "wayback": try_wayback,
    }

    errors = []
    for svc_name in services:
        func = service_map.get(svc_name)
        if not func:
            continue

        result = await func(url, timeout=timeout)
        if result.ok:
            logger.info(f"Archived {url} via {result.service}: {result.url}")
            return result
        errors.append(result.error)

    # All services failed - that's okay
    combined = "; ".join(filter(None, errors))
    logger.info(f"No archive for {url}: {combined}")
    return ArchiveResult(error=combined or "All archive services failed")
