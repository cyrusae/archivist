"""Tests for the SSRF guard.

Mocks DNS at the `asyncio.get_running_loop().getaddrinfo` layer so the suite
can run offline and deterministically across machines with different resolver
behavior.
"""

import asyncio
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from net import (  # noqa: E402
    UnsafeURLError,
    _disallowed,
    validate_public_url,
)
import ipaddress  # noqa: E402


# --- helpers ---------------------------------------------------------------


class _FakeLoop:
    """Async-compatible loop stub that returns scripted getaddrinfo results."""

    def __init__(self, mapping):
        self._mapping = mapping

    async def getaddrinfo(self, host, port, *, type=0, **kwargs):
        if host not in self._mapping:
            raise socket.gaierror(f"no mapping for {host!r}")
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", (addr, 0))
            for addr in self._mapping[host]
        ]


@pytest.fixture
def fake_dns(monkeypatch):
    """Return a callable that installs a fake DNS mapping for this test."""

    def install(mapping):
        loop = _FakeLoop(mapping)
        monkeypatch.setattr("net.asyncio.get_running_loop", lambda: loop)

    return install


def _run(coro):
    return asyncio.run(coro)


# --- _disallowed ------------------------------------------------------------


@pytest.mark.parametrize(
    "addr",
    [
        "127.0.0.1",           # loopback
        "10.0.0.5",            # RFC1918
        "172.16.0.1",          # RFC1918
        "192.168.1.1",         # RFC1918
        "169.254.169.254",     # link-local / AWS metadata
        "224.0.0.1",           # multicast
        "0.0.0.0",             # unspecified
        "::1",                 # IPv6 loopback
        "fe80::1",             # IPv6 link-local
        "fc00::1",             # IPv6 ULA (private)
    ],
)
def test_disallowed_covers_private_space(addr):
    assert _disallowed(ipaddress.ip_address(addr)) is True


@pytest.mark.parametrize(
    "addr",
    [
        "8.8.8.8",
        "1.1.1.1",
        "93.184.216.34",     # example.com
        "2606:4700:4700::1111",  # cloudflare v6
    ],
)
def test_disallowed_lets_public_through(addr):
    assert _disallowed(ipaddress.ip_address(addr)) is False


# --- validate_public_url: scheme + structure -------------------------------


def test_rejects_non_http_scheme():
    with pytest.raises(UnsafeURLError, match="scheme"):
        _run(validate_public_url("file:///etc/passwd"))


def test_rejects_gopher():
    with pytest.raises(UnsafeURLError, match="scheme"):
        _run(validate_public_url("gopher://example.com/"))


def test_rejects_no_host():
    with pytest.raises(UnsafeURLError, match="host"):
        _run(validate_public_url("http:///path"))


# --- validate_public_url: IP literals (no DNS) -----------------------------


def test_rejects_loopback_literal(fake_dns):
    fake_dns({})  # DNS should not be consulted at all
    with pytest.raises(UnsafeURLError, match="non-public IP literal"):
        _run(validate_public_url("http://127.0.0.1:5432/"))


def test_rejects_aws_metadata_literal(fake_dns):
    fake_dns({})
    with pytest.raises(UnsafeURLError, match="non-public IP literal"):
        _run(validate_public_url("http://169.254.169.254/latest/meta-data/"))


def test_rejects_rfc1918_literal(fake_dns):
    fake_dns({})
    with pytest.raises(UnsafeURLError, match="non-public IP literal"):
        _run(validate_public_url("http://10.0.0.1/"))


def test_rejects_ipv6_loopback_literal(fake_dns):
    fake_dns({})
    with pytest.raises(UnsafeURLError, match="non-public IP literal"):
        _run(validate_public_url("http://[::1]/"))


def test_accepts_public_ipv4_literal(fake_dns):
    fake_dns({})
    _run(validate_public_url("http://8.8.8.8/"))  # no raise


# --- validate_public_url: hostnames (DNS) ----------------------------------


def test_accepts_public_hostname(fake_dns):
    fake_dns({"example.com": ["93.184.216.34"]})
    _run(validate_public_url("https://example.com/path"))


def test_rejects_hostname_resolving_to_loopback(fake_dns):
    fake_dns({"evil.test": ["127.0.0.1"]})
    with pytest.raises(UnsafeURLError, match="non-public"):
        _run(validate_public_url("http://evil.test/"))


def test_rejects_hostname_resolving_to_cluster_dns(fake_dns):
    # This is the K8s-internal SSRF scenario from the review.
    fake_dns({"postgres.archivist.svc.cluster.local": ["10.42.0.17"]})
    with pytest.raises(UnsafeURLError, match="non-public"):
        _run(
            validate_public_url(
                "http://postgres.archivist.svc.cluster.local:5432/"
            )
        )


def test_rejects_hostname_with_mixed_results(fake_dns):
    # Conservative: if *any* record is private we reject. Defeats DNS
    # round-robin tricks that mix one public IP with one private.
    fake_dns({"sneaky.test": ["8.8.8.8", "127.0.0.1"]})
    with pytest.raises(UnsafeURLError, match="non-public"):
        _run(validate_public_url("http://sneaky.test/"))


def test_rejects_unresolvable_hostname(fake_dns):
    fake_dns({})  # gaierror on lookup
    with pytest.raises(UnsafeURLError, match="DNS"):
        _run(validate_public_url("http://no-such.invalid/"))


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
