"""SSRF guard + redirect-safe HTTP fetch.

Every outbound fetch that takes a user-supplied URL must go through
`validate_public_url` (for a one-shot check) or `safe_get` (for a full
request with per-hop redirect validation and a streaming size cap).

Design decisions:

- Reject conservatively: if *any* A/AAAA record for a hostname is private /
  loopback / link-local / multicast / reserved / unspecified, the whole URL
  fails. This defeats DNS round-robin SSRF tricks that return a mix of public
  and private addresses.
- Validate each redirect hop before connecting. aiohttp's `allow_redirects=True`
  follows redirects internally — by the time we inspect `resp.url`, the TCP
  connection has already happened. So we do `allow_redirects=False` and loop
  manually.
- There is a residual DNS-rebinding window between our validation and aiohttp's
  own resolution. For a homelab bot this is acceptable; a production fix would
  pin the resolved IP and send `Host:` manually, at the cost of breaking
  HTTPS SNI. Not worth it here.
- Playwright is *not* covered by this module — it does its own DNS and
  follows redirects in-browser. `snapshot.py` validates the initial URL only.
"""

import asyncio
import ipaddress
import logging
import socket
from typing import List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import aiohttp

logger = logging.getLogger("archivist.net")

ALLOWED_SCHEMES = frozenset({"http", "https"})
DNS_TIMEOUT = 5.0
MAX_REDIRECTS = 5
DEFAULT_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB


class UnsafeURLError(ValueError):
    """Raised when a URL targets a non-public host or unsupported scheme."""


class ResponseTooLarge(Exception):
    """Raised mid-stream when the body exceeds the configured cap."""


def _disallowed(ip) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def _resolve(host: str) -> List[str]:
    """Return the set of IP address strings `host` resolves to.

    Raises `UnsafeURLError` on DNS failure or empty result — callers treat DNS
    failure as "do not fetch," which is the safe default.
    """
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, None, type=socket.SOCK_STREAM),
            timeout=DNS_TIMEOUT,
        )
    except (socket.gaierror, asyncio.TimeoutError) as e:
        raise UnsafeURLError(
            f"DNS resolution failed for {host!r}: {type(e).__name__}"
        )

    addrs: List[str] = []
    for _family, _type, _proto, _canonname, sockaddr in infos:
        addrs.append(sockaddr[0])
    if not addrs:
        raise UnsafeURLError(f"No addresses returned for {host!r}")
    return addrs


async def validate_public_url(url: str) -> None:
    """Raise UnsafeURLError unless `url` is safe to fetch.

    Safe = http(s) scheme, has a hostname, and every resolved IP is publicly
    routable. IP literals are short-circuited (no DNS round trip).
    """
    try:
        parsed = urlparse(url)
    except Exception as e:  # pragma: no cover — urlparse is very permissive
        raise UnsafeURLError(f"Could not parse URL: {type(e).__name__}")

    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeURLError(f"scheme not allowed: {scheme!r}")

    host = parsed.hostname
    if not host:
        raise UnsafeURLError("URL has no host")

    # Short-circuit bracketed or bare IP literals.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is not None:
        if _disallowed(literal):
            raise UnsafeURLError(f"non-public IP literal: {host}")
        return

    addrs = await _resolve(host)
    bad: List[str] = []
    for a in addrs:
        try:
            ip = ipaddress.ip_address(a)
        except ValueError:
            continue
        if _disallowed(ip):
            bad.append(a)
    if bad:
        raise UnsafeURLError(
            f"{host!r} resolves to non-public address(es): {', '.join(bad)}"
        )


async def safe_get(
    session: aiohttp.ClientSession,
    url: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_redirects: int = MAX_REDIRECTS,
) -> Tuple[aiohttp.ClientResponse, bytes, str]:
    """Fetch `url` with per-hop SSRF validation and a streaming size cap.

    Returns `(final_response, body_bytes, final_url)`. The response is already
    released by the time this returns — callers should read headers off the
    returned object but not call `.read()` again.

    Raises:
        UnsafeURLError: if any URL in the redirect chain targets a non-public host.
        ResponseTooLarge: if the streamed body exceeds `max_bytes`.
        aiohttp.ClientError: for transport-level failures.
    """
    await validate_public_url(url)

    current = url
    for hop in range(max_redirects + 1):
        # NB: allow_redirects=False so we can validate every hop before any
        # new connection happens.
        resp = await session.get(current, allow_redirects=False)
        try:
            if 300 <= resp.status < 400 and resp.status != 304:
                if hop == max_redirects:
                    raise aiohttp.ClientError(
                        f"too many redirects (>{max_redirects})"
                    )
                loc = resp.headers.get("Location")
                if not loc:
                    raise aiohttp.ClientError(
                        f"HTTP {resp.status} with no Location header"
                    )
                # Resolve relative redirects against the URL we actually hit.
                current = urljoin(str(resp.url), loc)
                await validate_public_url(current)
                continue

            # Non-redirect status → stream the body with a byte cap.
            chunks: List[bytes] = []
            total = 0
            async for chunk in resp.content.iter_chunked(64 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise ResponseTooLarge(
                        f"response exceeded {max_bytes} bytes"
                    )
                chunks.append(chunk)
            return resp, b"".join(chunks), str(resp.url)
        finally:
            resp.release()

    # Unreachable — loop either returns or raises — but keep mypy happy.
    raise aiohttp.ClientError("redirect loop exited without response")


def decode_body(body: bytes, content_type: Optional[str]) -> str:
    """Decode a body to text using the charset hint from Content-Type, falling
    back to utf-8 with lenient replacement."""
    charset = "utf-8"
    if content_type:
        for part in content_type.split(";"):
            part = part.strip().lower()
            if part.startswith("charset="):
                charset = part.split("=", 1)[1].strip().strip('"') or "utf-8"
                break
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")
