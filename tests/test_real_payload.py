"""
Replay test using the ACTUAL response captured from a live `curl -i` against
crypto-sentiment-x402.onrender.com/sentiment/BTC on September 24, 2026 (the
reference implementation this whole checklist was built from).

This is the closest thing to an integration test this project has without
live network access from the sandbox that built it: the exact bytes a real
x402 seller returned, replayed through the real fetch->decode->diagnose
pipeline via a mocked transport. If this ever starts failing, either the
pipeline regressed or crypto-sentiment-x402's own deployment changed shape.
"""

import httpx
import pytest

import safe_fetch as sf
from diagnosis import Status
from dry_check import run_dry_check
from safe_fetch import ResolvedTarget

# Captured verbatim via `curl -i https://crypto-sentiment-x402.onrender.com/sentiment/BTC`
# on September 24, 2026. Body was literally `{}`; this is the real
# `payment-required` header value, unmodified.
REAL_PAYMENT_REQUIRED_HEADER = (
    "eyJ4NDAyVmVyc2lvbiI6MiwiZXJyb3IiOiJQYXltZW50IHJlcXVpcmVkIiwicmVzb3VyY2UiOnsidXJsIjoi"
    "aHR0cHM6Ly9jcnlwdG8tc2VudGltZW50LXg0MDIub25yZW5kZXIuY29tL3NlbnRpbWVudC9CVEMiLCJkZXNj"
    "cmlwdGlvbiI6IlJlYWwtdGltZSBjcnlwdG8gc2VudGltZW50IGZvciBhIHRpY2tlciBzeW1ib2wgKGUuZy4g"
    "QlRDLCBFVEgsIFNPTCkuIEFnZ3JlZ2F0ZXMgMTAgY3J5cHRvIG5ld3MgUlNTIG91dGxldHMgKENvaW5EZXNr"
    "LCBDb2ludGVsZWdyYXBoLCBEZWNyeXB0LCBCaXRjb2luIE1hZ2F6aW5lLCBUaGUgQmxvY2ssIENyeXB0b1Ns"
    "YXRlLCBOZXdzQlRDLCBDcnlwdG9Qb3RhdG8sIFRoZSBEZWZpYW50LCBETCBOZXdzKSBhbmQgdGhlIEZlYXIg"
    "JiBHcmVlZCBJbmRleC4gUmV0dXJucyBhIGJ1bGxpc2gvYmVhcmlzaC9uZXV0cmFsIGxhYmVsLCBzZW50aW1l"
    "bnQgc2NvcmUsIGFuZCBwZXItc291cmNlIGJyZWFrZG93biBhcyBKU09OLiBVc2VmdWwgZm9yIHRyYWRpbmcg"
    "Ym90cyBhbmQgbWFya2V0IHJlc2VhcmNoIGFnZW50cy4gUGF0aCBwYXJhbTogc3ltYm9sLCBlLmcuIC9zZW50"
    "aW1lbnQvQlRDLiIsIm1pbWVUeXBlIjoiYXBwbGljYXRpb24vanNvbiJ9LCJhY2NlcHRzIjpbeyJzY2hlbWUi"
    "OiJleGFjdCIsIm5ldHdvcmsiOiJlaXAxNTU6ODQ1MyIsImFzc2V0IjoiMHg4MzM1ODlmQ0Q2ZURiNkUwOGY0"
    "YzdDMzJENGY3MWI1NGJkQTAyOTEzIiwiYW1vdW50IjoiMTAwMDAiLCJwYXlUbyI6IjB4QTZFMTA4ODRhNzBE"
    "MDBERThiOGM1ZGZGMGEyYTlCMWE4MjFjRTA3NCIsIm1heFRpbWVvdXRTZWNvbmRzIjozMDAsImV4dHJhIjp7"
    "Im5hbWUiOiJVU0QgQ29pbiIsInZlcnNpb24iOiIyIn19XSwiZXh0ZW5zaW9ucyI6eyJiYXphYXIiOnsiaW5m"
    "byI6eyJpbnB1dCI6eyJ0eXBlIjoiaHR0cCIsInF1ZXJ5UGFyYW1zIjp7Im1ldGhvZCI6IkdFVCIsInN5bWJv"
    "bCI6IkJUQyJ9LCJtZXRob2QiOiJHRVQiLCJwYXRoUGFyYW1zIjp7InN5bWJvbCI6IkJUQyJ9fSwib3V0cHV0"
    "Ijp7InR5cGUiOiJqc29uIiwiZXhhbXBsZSI6eyJzeW1ib2wiOiJCVEMiLCJuYW1lIjoiQml0Y29pbiIsIm92"
    "ZXJhbGxfc2VudGltZW50Ijp7ImxhYmVsIjoiYnVsbGlzaCIsImF2ZXJhZ2VfY29tcG91bmQiOjAuMjF9fX19"
    "LCJzY2hlbWEiOnsiJHNjaGVtYSI6Imh0dHBzOi8vanNvbi1zY2hlbWEub3JnL2RyYWZ0LzIwMjAtMTIvc2No"
    "ZW1hIiwidHlwZSI6Im9iamVjdCIsInByb3BlcnRpZXMiOnsiaW5wdXQiOnsidHlwZSI6Im9iamVjdCIsInBy"
    "b3BlcnRpZXMiOnsidHlwZSI6eyJ0eXBlIjoic3RyaW5nIiwiY29uc3QiOiJodHRwIn0sIm1ldGhvZCI6eyJ0"
    "eXBlIjoic3RyaW5nIiwiZW51bSI6WyJHRVQiLCJIRUFEIiwiREVMRVRFIl19LCJxdWVyeVBhcmFtcyI6eyJ0"
    "eXBlIjoib2JqZWN0IiwicHJvcGVydGllcyI6eyJtZXRob2QiOnsidHlwZSI6InN0cmluZyIsImRlc2NyaXB0"
    "aW9uIjoiSFRUUCBtZXRob2QsIGFsd2F5cyBHRVQifSwic3ltYm9sIjp7InR5cGUiOiJzdHJpbmciLCJkZXNj"
    "cmlwdGlvbiI6IlVwcGVyY2FzZSB0aWNrZXIgc3ltYm9sLCBlLmcuIEJUQywgRVRILCBTT0wifX0sInJlcXVp"
    "cmVkIjpbIm1ldGhvZCIsInN5bWJvbCJdfSwicGF0aFBhcmFtcyI6eyJ0eXBlIjoib2JqZWN0In19LCJyZXF1"
    "aXJlZCI6WyJ0eXBlIiwibWV0aG9kIl0sImFkZGl0aW9uYWxQcm9wZXJ0aWVzIjpmYWxzZX0sIm91dHB1dCI6"
    "eyJ0eXBlIjoib2JqZWN0IiwicHJvcGVydGllcyI6eyJ0eXBlIjp7InR5cGUiOiJzdHJpbmcifSwiZXhhbXBs"
    "ZSI6eyJ0eXBlIjoib2JqZWN0IiwicHJvcGVydGllcyI6eyJzeW1ib2wiOnsidHlwZSI6InN0cmluZyJ9LCJu"
    "YW1lIjp7InR5cGUiOiJzdHJpbmcifSwib3ZlcmFsbF9zZW50aW1lbnQiOnsidHlwZSI6Im9iamVjdCJ9fSwi"
    "cmVxdWlyZWQiOlsic3ltYm9sIiwib3ZlcmFsbF9zZW50aW1lbnQiXX19LCJyZXF1aXJlZCI6WyJ0eXBlIl19"
    "fSwicmVxdWlyZWQiOlsiaW5wdXQiXX0sInJvdXRlVGVtcGxhdGUiOiIvc2VudGltZW50LzpzeW1ib2wifX19"
)


class _ChunkedStream(httpx.AsyncByteStream):
    def __init__(self, body: bytes, chunk_size: int = 4096):
        self._body = body
        self._chunk_size = chunk_size

    async def __aiter__(self):
        for i in range(0, len(self._body), self._chunk_size):
            yield self._body[i : i + self._chunk_size]

    async def aclose(self) -> None:
        return None


@pytest.fixture
def fake_resolver(monkeypatch):
    async def _fake(hostname: str, port: int) -> ResolvedTarget:
        return ResolvedTarget(hostname=hostname, port=port, ip="1.2.3.4", family=2)

    monkeypatch.setattr(sf, "resolve_and_validate", _fake)


async def test_real_crypto_sentiment_x402_payload_diagnoses_clean(fake_resolver, monkeypatch):
    """Replays the exact real 402 response and confirms every check either
    passes or (for the informational ones) has nothing to flag -- matching
    what's actually known about this deployment (a confirmed real mainnet
    settlement already worked; the only known issue is Bazaar indexing
    itself, which none of checks 1-5 can diagnose from a single 402 -- that
    needs the paid/settlement-echo and catalog-lookup checks this project
    hasn't built yet)."""
    import dry_check

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            402,
            headers={
                "content-type": "application/json",
                "payment-required": REAL_PAYMENT_REQUIRED_HEADER,
            },
            stream=_ChunkedStream(b"{}"),
        )

    transport = httpx.MockTransport(handler)

    async def _patched_safe_fetch(url, method="GET", **kwargs):
        return await sf.safe_fetch(url, method=method, transport=transport, **kwargs)

    monkeypatch.setattr(dry_check, "safe_fetch", _patched_safe_fetch)

    report = await run_dry_check("https://crypto-sentiment-x402.onrender.com/sentiment/BTC")

    assert report.http_status == 402
    assert report.parse_error is None

    statuses = {c.check_id: c.status for c in report.checks}
    assert statuses["resource_present"] == Status.PASS
    assert statuses["scheme_mismatch"] == Status.PASS
    assert statuses["bazaar_extension"] == Status.PASS
    assert statuses["description_length"] == Status.PASS
    # routeTemplate in the real payload is "/sentiment/:symbol" -- named
    # params, matching the memory note that this was already fixed from an
    # earlier wildcard-routing bug.
    assert statuses["route_template"] == Status.PASS

    assert not report.failures
    assert "No issues found" in report.verdict
