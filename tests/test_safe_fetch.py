"""
Tests for safe_fetch.py.

The blocklist and resolve_and_validate tests run offline using literal IP
addresses (127.0.0.1, 169.254.169.254, etc.) and "localhost", none of which
need real DNS or network I/O -- socket.getaddrinfo resolves those locally in
every environment.

The safe_fetch() tests replace the network entirely with httpx.MockTransport
*and* monkeypatch resolve_and_validate to a fake resolver, so they don't
depend on what "example.com" actually resolves to in whatever sandbox this
runs in -- the fake resolver always returns 1.2.3.4, and the mock transport
handler asserts every request actually landed on that IP.
"""

import ipaddress

import httpx
import pytest

import safe_fetch as sf
from safe_fetch import (
    FetchError,
    SSRFBlocked,
    ResolvedTarget,
    is_blocked,
    resolve_and_validate,
    safe_fetch,
)

# asyncio_mode = "auto" (pyproject.toml) detects async def tests on its own,
# so sync tests below aren't spuriously marked as asyncio tests.


class _ChunkedStream(httpx.AsyncByteStream):
    """A minimal AsyncByteStream for building MockTransport responses that
    actually stream, instead of using httpx.Response's `content=`/`json=`
    shortcuts.

    Those shortcuts fully materialize `_content` up front and mark the
    response's stream as already consumed -- fine for `aiter_bytes()`,
    which special-cases pre-materialized content, but it means
    `aiter_raw()` (what safe_fetch._read_capped actually reads from) would
    raise StreamConsumed immediately. A real HTTP transport streams lazily
    from the socket and never hits that shortcut, so building responses
    this way in tests is what makes them representative of production
    instead of accidentally testing a code path that only exists for
    already-fully-buffered mock responses.
    """

    def __init__(self, body: bytes, chunk_size: int = 4096):
        self._body = body
        self._chunk_size = chunk_size

    async def __aiter__(self):
        for i in range(0, len(self._body), self._chunk_size):
            yield self._body[i : i + self._chunk_size]

    async def aclose(self) -> None:
        return None


def _streaming_response(status_code, body: bytes = b"", headers=None) -> httpx.Response:
    return httpx.Response(status_code, headers=headers, stream=_ChunkedStream(body))


# --------------------------------------------------------------------------
# Blocklist
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ip_str",
    [
        "127.0.0.1",  # loopback
        "10.1.2.3",  # RFC1918
        "172.16.0.5",  # RFC1918
        "192.168.1.1",  # RFC1918
        "169.254.169.254",  # cloud metadata
        "169.254.1.1",  # link-local
        "0.0.0.0",
        "100.64.0.1",  # CGNAT
        "224.0.0.1",  # multicast
        "255.255.255.255",
        "::1",  # IPv6 loopback
        "fc00::1",  # unique local
        "fd00:ec2::254",  # AWS IMDSv2 IPv6 (falls under fc00::/7)
        "fe80::1",  # link-local
        "64:ff9b::0101:0101",  # NAT64-mapped address
    ],
)
def test_known_bad_ips_are_blocked(ip_str):
    assert is_blocked(ipaddress.ip_address(ip_str)) is True


@pytest.mark.parametrize(
    "ip_str",
    [
        "1.1.1.1",
        "8.8.8.8",
        "93.184.216.34",  # example.com-ish public IP
        "2606:4700:4700::1111",  # Cloudflare public IPv6
    ],
)
def test_known_good_ips_are_not_blocked(ip_str):
    assert is_blocked(ipaddress.ip_address(ip_str)) is False


# --------------------------------------------------------------------------
# resolve_and_validate -- literal IPs / localhost skip real DNS
# --------------------------------------------------------------------------


async def test_resolve_and_validate_blocks_loopback():
    with pytest.raises(SSRFBlocked):
        await resolve_and_validate("127.0.0.1", 80)


async def test_resolve_and_validate_blocks_metadata_ip():
    with pytest.raises(SSRFBlocked):
        await resolve_and_validate("169.254.169.254", 80)


async def test_resolve_and_validate_blocks_localhost_hostname():
    # "localhost" resolves via /etc/hosts to 127.0.0.1 -- no network needed,
    # and it's exactly the kind of hostname (not a literal IP) an attacker
    # would use to test whether validation is IP-based or string-based.
    with pytest.raises(SSRFBlocked):
        await resolve_and_validate("localhost", 80)


async def test_resolve_and_validate_allows_public_literal_ip():
    resolved = await resolve_and_validate("1.1.1.1", 443)
    assert resolved.ip == "1.1.1.1"


async def test_resolve_and_validate_raises_fetcherror_on_bad_hostname():
    with pytest.raises(FetchError):
        await resolve_and_validate("this-host-does-not-exist.invalid", 80)


# --------------------------------------------------------------------------
# safe_fetch, driven by a MockTransport + a monkeypatched resolver
# --------------------------------------------------------------------------


@pytest.fixture
def fake_resolver(monkeypatch):
    """Force every hostname to resolve to the fixed public IP 1.2.3.4, so
    tests don't depend on real DNS for made-up hostnames like example.com.
    """

    async def _fake(hostname: str, port: int) -> ResolvedTarget:
        return ResolvedTarget(hostname=hostname, port=port, ip="1.2.3.4", family=2)

    monkeypatch.setattr(sf, "resolve_and_validate", _fake)


async def test_safe_fetch_happy_path(fake_resolver):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "1.2.3.4"  # connected by IP, not hostname
        assert request.headers["host"] == "example.com"  # original Host preserved
        assert request.extensions.get("sni_hostname") == "example.com"
        return _streaming_response(200, b'{"ok": true}')

    transport = httpx.MockTransport(handler)
    resp = await safe_fetch("https://example.com/resource", transport=transport)
    assert resp.status_code == 200
    assert b"ok" in resp.body


async def test_safe_fetch_rejects_redirect_to_blocked_ip(monkeypatch):
    """A safe, public initial URL that redirects to a metadata/internal
    hostname must be blocked at the redirect hop, not just at the start --
    this exercises the real (unpatched) resolver for the second hop, since
    the whole point is that resolve_and_validate runs again after the
    redirect and catches it there.
    """

    async def _fake_first_hop(hostname: str, port: int) -> ResolvedTarget:
        if hostname == "example.com":
            return ResolvedTarget(hostname=hostname, port=port, ip="1.2.3.4", family=2)
        # second hop: fall through to the real resolver so the blocklist
        # actually runs against the redirect target
        return await resolve_and_validate(hostname, port)

    monkeypatch.setattr(sf, "resolve_and_validate", _fake_first_hop)

    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if request.url.host == "1.2.3.4":
            return _streaming_response(
                302, headers={"location": "http://169.254.169.254/secret"}
            )
        raise AssertionError(f"should never connect to {request.url.host}")

    transport = httpx.MockTransport(handler)
    with pytest.raises(SSRFBlocked):
        await safe_fetch("http://example.com/start", transport=transport)

    # only the first (safe) hop should have actually been dispatched
    assert call_count["n"] == 1


async def test_safe_fetch_follows_safe_redirect_chain(fake_resolver):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["host"] == "a.example.com":
            return _streaming_response(302, headers={"location": "https://b.example.com/final"})
        if request.headers["host"] == "b.example.com":
            return _streaming_response(200, b'{"final": true}')
        raise AssertionError("unexpected request")

    transport = httpx.MockTransport(handler)
    resp = await safe_fetch("https://a.example.com/start", transport=transport)
    assert resp.status_code == 200
    assert resp.redirect_chain == ["https://a.example.com/start"]


async def test_safe_fetch_enforces_max_redirects(fake_resolver):
    hop_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        hop_count["n"] += 1
        return _streaming_response(302, headers={"location": "https://example.com/loop"})

    transport = httpx.MockTransport(handler)
    with pytest.raises(FetchError, match="redirects"):
        await safe_fetch("https://example.com/loop", transport=transport, max_redirects=3)

    # initial request + 3 redirect hops = 4 dispatched requests
    assert hop_count["n"] == 4


async def test_safe_fetch_enforces_wire_size_cap(fake_resolver):
    big_body = b"x" * 10_000

    def handler(request: httpx.Request) -> httpx.Response:
        return _streaming_response(200, big_body)

    transport = httpx.MockTransport(handler)
    with pytest.raises(FetchError, match="wire-size"):
        await safe_fetch(
            "https://example.com/big", transport=transport, max_wire_bytes=1_000
        )


async def test_safe_fetch_rejects_unsupported_scheme(fake_resolver):
    with pytest.raises(FetchError):
        await safe_fetch("ftp://example.com/file")


async def test_safe_fetch_enforces_decompressed_size_cap_on_gzip_bomb(fake_resolver):
    """A small gzip payload that decompresses far past the cap must be
    caught during decompression, not after the (small) wire transfer
    completes -- this is the actual decompression-bomb guard."""
    import gzip

    huge = b"0" * 5_000_000  # decompresses to 5MB from a tiny gzip stream
    compressed = gzip.compress(huge)
    assert len(compressed) < 10_000  # confirms this really is a small payload on the wire

    def handler(request: httpx.Request) -> httpx.Response:
        return _streaming_response(
            200, compressed, headers={"content-encoding": "gzip"}
        )

    transport = httpx.MockTransport(handler)
    with pytest.raises(FetchError, match="decompressed-size"):
        await safe_fetch(
            "https://example.com/bomb",
            transport=transport,
            max_wire_bytes=1_000_000,
            max_decompressed_bytes=100_000,
        )


async def test_safe_fetch_decompresses_gzip_within_caps(fake_resolver):
    import gzip

    payload = b'{"hello": "world"}'
    compressed = gzip.compress(payload)

    def handler(request: httpx.Request) -> httpx.Response:
        return _streaming_response(
            200, compressed, headers={"content-encoding": "gzip"}
        )

    transport = httpx.MockTransport(handler)
    resp = await safe_fetch("https://example.com/ok-gzip", transport=transport)
    assert resp.body == payload


async def test_safe_fetch_rejects_unsupported_content_encoding(fake_resolver):
    def handler(request: httpx.Request) -> httpx.Response:
        return _streaming_response(200, b"whatever", headers={"content-encoding": "br"})

    transport = httpx.MockTransport(handler)
    with pytest.raises(FetchError, match="content-encoding"):
        await safe_fetch("https://example.com/brotli", transport=transport)
