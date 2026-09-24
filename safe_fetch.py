"""
SSRF-safe fetcher for x402 Doctor.

x402 Doctor fetches user-submitted URLs, which is a genuine SSRF attack
surface: a target could point at a cloud metadata endpoint, an internal
service, or use DNS rebinding to swap a public IP for a private one between
our check and our actual connection.

The naive fix ("does this hostname resolve to a private IP?" then fetch by
hostname) is broken because of that race: DNS can answer differently the
second time. This module closes that gap by resolving the hostname exactly
once, validating the resolved IP, and then connecting directly to that
validated IP for the rest of the request -- never re-resolving the hostname.
Redirects are not followed automatically; each hop re-runs the full
resolve-validate-connect sequence, because a redirect can point anywhere
regardless of how safe the original URL was.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import zlib
from dataclasses import dataclass, field
from typing import Optional, Union

import httpx

IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]


class SSRFBlocked(Exception):
    """Raised when a target resolves to, or redirects to, a blocked address."""


class FetchError(Exception):
    """Raised for any other fetch failure: timeout, size cap, malformed
    response, unsupported scheme. Callers should treat this as "couldn't
    check this endpoint" rather than a diagnosis result."""


# --------------------------------------------------------------------------
# Blocklist
# --------------------------------------------------------------------------
# Beyond the ranges the spec calls out by name (RFC1918, loopback, link-local
# / cloud metadata, 0.0.0.0, unique-local IPv6), this also blocks CGNAT space,
# multicast/reserved ranges, and the NAT64 well-known prefix (64:ff9b::/96),
# which can be used to address IPv4-mapped internal hosts from an IPv6-only
# client. ipaddress's own is_private/is_loopback/is_link_local/is_multicast/
# is_reserved/is_unspecified flags already cover most of this; the explicit
# network list exists so the policy is legible and testable on its own,
# rather than resting entirely on stdlib flag semantics.

_BLOCKED_NETWORKS: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    # IPv4
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),  # carrier-grade NAT
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local; covers 169.254.169.254
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),  # IETF protocol assignments
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),  # benchmarking
    ipaddress.ip_network("224.0.0.0/4"),  # multicast
    ipaddress.ip_network("240.0.0.0/4"),  # reserved
    ipaddress.ip_network("255.255.255.255/32"),
    # IPv6
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("64:ff9b::/96"),  # NAT64 well-known prefix
    ipaddress.ip_network("fc00::/7"),  # unique local (covers fd00:ec2::254)
    ipaddress.ip_network("fe80::/10"),  # link-local
    ipaddress.ip_network("2001:db8::/32"),  # documentation-only, never legitimate live traffic
]


def is_blocked(ip: IPAddress) -> bool:
    """True if `ip` must never be connected to by this service."""
    if (
        ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_private
    ):
        return True
    return any(ip in net for net in _BLOCKED_NETWORKS)


# --------------------------------------------------------------------------
# Resolution + validation
# --------------------------------------------------------------------------


@dataclass
class ResolvedTarget:
    hostname: str
    port: int
    ip: str  # validated, safe IP as a string
    family: int  # socket.AF_INET or socket.AF_INET6


async def resolve_and_validate(hostname: str, port: int) -> ResolvedTarget:
    """Resolve `hostname` and return the first candidate IP that passes the
    blocklist. Raises SSRFBlocked if every candidate is blocked, FetchError
    if resolution itself fails.

    This is the ONLY place DNS is consulted for a given hop. The caller must
    connect to the returned IP directly and must not re-resolve the hostname
    -- doing so would reopen exactly the DNS-rebinding gap this module
    exists to close.
    """
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise FetchError(f"DNS resolution failed for {hostname!r}: {e}") from e

    if not infos:
        raise FetchError(f"No addresses returned for {hostname!r}")

    candidates: list[tuple[int, str]] = [
        (family, sockaddr[0]) for family, _, _, _, sockaddr in infos
    ]

    for family, ip_str in candidates:
        ip_obj = ipaddress.ip_address(ip_str)
        if not is_blocked(ip_obj):
            return ResolvedTarget(hostname=hostname, port=port, ip=ip_str, family=family)

    raise SSRFBlocked(
        f"All resolved addresses for {hostname!r} are blocked: "
        f"{[ip for _, ip in candidates]}"
    )


# --------------------------------------------------------------------------
# Request building
# --------------------------------------------------------------------------


def _request_to_resolved_ip(
    client: httpx.AsyncClient,
    method: str,
    url: httpx.URL,
    resolved: ResolvedTarget,
    headers: Optional[dict] = None,
) -> httpx.Request:
    """Build a request that connects to the validated IP directly while
    still presenting the original hostname as the Host header and TLS SNI.
    This is the piece that keeps the check meaningful under DNS rebinding:
    validation and connection use the same IP because we never look the
    hostname up a second time.
    """
    ip_url = url.copy_with(host=resolved.ip)
    req = client.build_request(method, ip_url, headers=headers or {})
    req.headers["host"] = url.host
    req.extensions["sni_hostname"] = url.host
    return req


# --------------------------------------------------------------------------
# Public fetch API
# --------------------------------------------------------------------------


@dataclass
class SafeResponse:
    status_code: int
    headers: httpx.Headers
    url: str  # final URL after any redirects
    body: bytes
    redirect_chain: list[str] = field(default_factory=list)


async def safe_fetch(
    url: str,
    method: str = "GET",
    headers: Optional[dict] = None,
    max_redirects: int = 5,
    connect_timeout: float = 5.0,
    total_timeout: float = 15.0,
    max_wire_bytes: int = 2_000_000,
    max_decompressed_bytes: int = 10_000_000,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> SafeResponse:
    """Fetch `url` the SSRF-safe way.

    - Resolves and validates before every connection, including after each
      redirect hop (redirects are handled manually; httpx's automatic
      following is disabled on purpose).
    - Enforces a separate connect timeout and total-request timeout.
    - Caps wire bytes and decompressed bytes independently, so a small
      compressed payload that expands into gigabytes is caught before it
      fully inflates in memory (a decompression-bomb guard).

    `transport` is exposed for testing (e.g. httpx.MockTransport); leave it
    unset for real network use.
    """
    redirect_chain: list[str] = []
    current_url = httpx.URL(url)

    if current_url.scheme not in ("http", "https"):
        raise FetchError(f"Unsupported scheme: {current_url.scheme!r}")

    timeout = httpx.Timeout(total_timeout, connect=connect_timeout)

    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,  # we re-validate and follow manually
        transport=transport,
    ) as client:
        for hop in range(max_redirects + 1):
            if current_url.scheme not in ("http", "https"):
                raise SSRFBlocked(
                    f"Redirect to unsupported scheme: {current_url.scheme!r}"
                )

            port = current_url.port or (443 if current_url.scheme == "https" else 80)
            resolved = await resolve_and_validate(current_url.host, port)

            req = _request_to_resolved_ip(client, method, current_url, resolved, headers)

            try:
                response = await client.send(req, stream=True)
            except httpx.TimeoutException as e:
                raise FetchError(f"Timeout fetching {current_url}: {e}") from e
            except httpx.HTTPError as e:
                raise FetchError(f"Transport error fetching {current_url}: {e}") from e

            try:
                body = await _read_capped(response, max_wire_bytes, max_decompressed_bytes)
            finally:
                await response.aclose()

            if response.is_redirect:
                if hop >= max_redirects:
                    raise FetchError(f"Exceeded max redirects ({max_redirects})")
                location = response.headers.get("location")
                if not location:
                    raise FetchError("Redirect response missing Location header")
                redirect_chain.append(str(current_url))
                current_url = current_url.join(location)
                continue

            return SafeResponse(
                status_code=response.status_code,
                headers=response.headers,
                url=str(current_url),
                body=body,
                redirect_chain=redirect_chain,
            )

        raise FetchError(f"Exceeded max redirects ({max_redirects})")  # pragma: no cover


# Content-Encodings we know how to decompress ourselves. Anything else
# (brotli, zstd, ...) is rejected outright rather than handed to a decoder
# we don't control the cap on -- a target advertising an unsupported
# encoding gets a FetchError, not a silent unbounded decompression.
_SUPPORTED_ENCODINGS = {"", "identity", "gzip", "x-gzip", "deflate"}


async def _read_capped(
    response: httpx.Response, max_wire_bytes: int, max_decompressed_bytes: int
) -> bytes:
    """Stream and decompress the body ourselves, enforcing wire-size and
    decompressed-size caps independently, chunk by chunk.

    This deliberately does NOT use httpx's built-in `aiter_bytes()` (which
    auto-decompresses) or `response.num_bytes_downloaded` (which tracks
    real transport-layer byte counts and is a no-op under things like
    MockTransport, and whose exact accounting isn't a documented, stable
    contract to depend on for a security boundary). Instead it reads raw
    wire bytes via `aiter_raw()` and decompresses them itself with zlib,
    bounding each decompress() call's output so a small compressed chunk
    can never expand past the cap before we notice -- that per-call bound
    is what actually stops a decompression bomb, not just the cap check
    that runs after the fact.
    """
    encoding = response.headers.get("content-encoding", "").strip().lower()
    if encoding not in _SUPPORTED_ENCODINGS:
        raise FetchError(f"Unsupported content-encoding: {encoding!r}")

    decompressor: Optional[zlib.decompressobj] = None
    if encoding in ("gzip", "x-gzip"):
        decompressor = zlib.decompressobj(zlib.MAX_WBITS | 16)
    elif encoding == "deflate":
        decompressor = zlib.decompressobj()

    wire_bytes = 0
    decompressed = bytearray()

    async for chunk in response.aiter_raw():
        wire_bytes += len(chunk)
        if wire_bytes > max_wire_bytes:
            raise FetchError(f"Response exceeded wire-size cap of {max_wire_bytes} bytes")

        if decompressor is None:
            piece = chunk
        else:
            remaining = max_decompressed_bytes - len(decompressed) + 1
            try:
                piece = decompressor.decompress(chunk, remaining)
            except zlib.error as e:
                raise FetchError(f"Failed to decompress response body: {e}") from e

        decompressed.extend(piece)
        if len(decompressed) > max_decompressed_bytes:
            raise FetchError(
                f"Response exceeded decompressed-size cap of {max_decompressed_bytes} bytes"
            )

    if decompressor is not None:
        try:
            tail = decompressor.flush()
        except zlib.error as e:
            raise FetchError(f"Failed to decompress response body: {e}") from e
        decompressed.extend(tail)
        if len(decompressed) > max_decompressed_bytes:
            raise FetchError(
                f"Response exceeded decompressed-size cap of {max_decompressed_bytes} bytes"
            )

    return bytes(decompressed)


# --------------------------------------------------------------------------
# Reusable SSRF-safe transport (for clients we don't drive ourselves)
# --------------------------------------------------------------------------


class _WireCappedStream(httpx.AsyncByteStream):
    """Wraps another AsyncByteStream and raises FetchError once more than
    `max_bytes` wire bytes have been seen. Must subclass httpx.AsyncByteStream
    -- httpx's internal request-sending code asserts isinstance(stream,
    AsyncByteStream) on whatever a transport returns, and (confirmed by
    testing) reassigning an existing httpx.Response's `.stream` attribute
    after construction does *not* reliably take effect for later reads --
    the capped stream has to be passed into a *new* httpx.Response at
    construction time instead, which is what SSRFSafeTransport does below.
    """

    def __init__(self, inner: httpx.AsyncByteStream, max_bytes: int):
        self._inner = inner
        self._max_bytes = max_bytes

    async def __aiter__(self):
        total = 0
        async for chunk in self._inner:
            total += len(chunk)
            if total > self._max_bytes:
                raise FetchError(f"Response exceeded wire-size cap of {self._max_bytes} bytes")
            yield chunk

    async def aclose(self) -> None:
        aclose = getattr(self._inner, "aclose", None)
        if aclose is not None:
            await aclose()


class SSRFSafeTransport(httpx.AsyncBaseTransport):
    """A reusable, general-purpose SSRF-safe httpx transport.

    `safe_fetch()` above drives its own single request/redirect loop and
    can resolve-validate-connect once per hop because it owns that loop.
    This transport is for the opposite situation: handing a target URL to
    a third-party client that drives its own request lifecycle (this
    project's outbound x402 payment client, specifically, which needs to
    send an initial request, inspect a 402, and retry with a payment
    header -- all machinery this transport has no visibility into). So
    instead of a loop, this re-resolves and IP-pins *every single request*
    handed to it, independently, via the same resolve_and_validate() used
    above -- including a request that turns out to be a redirect or a
    payment retry, since each is a fresh call to handle_async_request.

    Two consequences worth knowing before pointing a new client at this:

    - If the owning httpx.AsyncClient sets follow_redirects=True, each
      redirect hop is a new handle_async_request call and gets
      independently validated (the same DNS-rebinding protection
      safe_fetch() has, enforced at the transport layer instead of a
      manual loop) -- but max_redirects must still be capped by the
      caller; this transport doesn't cap hop count itself.
    - Response bodies are capped at `max_wire_bytes` on the wire, but --
      unlike safe_fetch()'s _read_capped -- this does NOT defend against a
      decompression bomb: whatever reads the response (e.g. the x402 SDK)
      goes through httpx's ordinary automatic decompression, not the
      bounded zlib loop above. Acceptable for the traffic this transport
      is built to carry (a 402 challenge and a small paid-resource
      response, not an arbitrary page fetch) but a real, deliberate gap,
      not an oversight -- documented here so it isn't mistaken for the
      same guarantee safe_fetch() provides.
    """

    def __init__(
        self,
        max_wire_bytes: int = 5_000_000,
        connect_timeout: float = 5.0,
        inner_transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        self._inner = inner_transport or httpx.AsyncHTTPTransport()
        self._max_wire_bytes = max_wire_bytes
        self._connect_timeout = connect_timeout

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.scheme not in ("http", "https"):
            raise SSRFBlocked(f"Unsupported scheme: {url.scheme!r}")

        port = url.port or (443 if url.scheme == "https" else 80)
        resolved = await resolve_and_validate(url.host, port)

        # request.content is only safe to read here because callers of this
        # transport (x402AsyncTransport, specifically) call request.aread()
        # before ever reaching a transport -- see outbound_payment.py.
        pinned_request = httpx.Request(
            method=request.method,
            url=url.copy_with(host=resolved.ip),
            headers=request.headers,
            content=request.content,
            extensions={**request.extensions, "sni_hostname": url.host},
        )
        pinned_request.headers["host"] = url.host

        response = await self._inner.handle_async_request(pinned_request)
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            stream=_WireCappedStream(response.stream, self._max_wire_bytes),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()
