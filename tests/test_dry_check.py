"""
End-to-end tests for run_dry_check: safe_fetch -> parse -> diagnosis,
wired together the way the real /diagnose endpoint will use them.
Network is replaced with httpx.MockTransport + a monkeypatched resolver,
same approach as test_safe_fetch.py.
"""

import json

import httpx
import pytest

import safe_fetch as sf
from diagnosis import Status
from dry_check import run_dry_check
from safe_fetch import ResolvedTarget
from tests.test_bazaar import FakeBazaarClient, make_response


@pytest.fixture
def fake_resolver(monkeypatch):
    async def _fake(hostname: str, port: int) -> ResolvedTarget:
        return ResolvedTarget(hostname=hostname, port=port, ip="1.2.3.4", family=2)

    monkeypatch.setattr(sf, "resolve_and_validate", _fake)


class _ChunkedStream(httpx.AsyncByteStream):
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


# Matches the CONFIRMED real shape (x402Version 2): resource/description/
# extensions at the challenge level, not nested per-accepts-entry. See
# diagnosis.py's module docstring for the real curl output this came from.
GOOD_CHALLENGE = {
    "x402Version": 2,
    "error": "Payment required",
    "resource": {
        "url": "https://api.example.com/data",
        "description": "Sentiment data for a given token.",
        "mimeType": "application/json",
    },
    "accepts": [
        {
            "scheme": "exact",
            "network": "eip155:8453",
            "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "amount": "10000",
            "payTo": "0xA6E10884a70D00DE8b8c5dfF0a2a9B1a821cE074",
            "maxTimeoutSeconds": 300,
            "extra": {"name": "USD Coin", "version": "2"},
        }
    ],
    "extensions": {
        "bazaar": {
            "info": {"input": {"type": "http", "method": "GET"}},
            "routeTemplate": "/data/:id",
        }
    },
}


async def test_dry_check_reports_clean_challenge(fake_resolver, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return _streaming_response(402, json.dumps(GOOD_CHALLENGE).encode())

    transport = httpx.MockTransport(handler)

    # run_dry_check calls safe_fetch directly, and safe_fetch's `transport`
    # kwarg isn't exposed through run_dry_check's own signature -- so we
    # monkeypatch dry_check's imported name to inject the mock transport.
    import dry_check

    async def _patched_safe_fetch(url, method="GET", **kwargs):
        return await sf.safe_fetch(url, method=method, transport=transport, **kwargs)

    monkeypatch.setattr(dry_check, "safe_fetch", _patched_safe_fetch)

    report = await run_dry_check("https://api.example.com/data")

    assert report.http_status == 402
    assert report.parse_error is None
    statuses = {c.check_id: c.status for c in report.checks}
    assert statuses["resource_present"] == Status.PASS
    assert statuses["scheme_mismatch"] == Status.PASS
    assert statuses["bazaar_extension"] == Status.PASS
    assert statuses["description_length"] == Status.PASS
    assert "No issues found" in report.verdict


async def test_dry_check_flags_scheme_mismatch_and_missing_bazaar(fake_resolver, monkeypatch):
    import dry_check

    broken = json.loads(json.dumps(GOOD_CHALLENGE))
    broken["resource"]["url"] = "http://api.example.com/data"  # served over https
    del broken["extensions"]

    def handler(request: httpx.Request) -> httpx.Response:
        return _streaming_response(402, json.dumps(broken).encode())

    transport = httpx.MockTransport(handler)

    async def _patched_safe_fetch(url, method="GET", **kwargs):
        return await sf.safe_fetch(url, method=method, transport=transport, **kwargs)

    monkeypatch.setattr(dry_check, "safe_fetch", _patched_safe_fetch)

    report = await run_dry_check("https://api.example.com/data")

    statuses = {c.check_id: c.status for c in report.checks}
    assert statuses["scheme_mismatch"] == Status.FAIL
    assert statuses["bazaar_extension"] == Status.FAIL
    assert len(report.failures) == 2
    assert "issue(s) found" in report.verdict


async def test_dry_check_handles_non_402_status(fake_resolver, monkeypatch):
    import dry_check

    def handler(request: httpx.Request) -> httpx.Response:
        return _streaming_response(200, b'{"ok": true}')

    transport = httpx.MockTransport(handler)

    async def _patched_safe_fetch(url, method="GET", **kwargs):
        return await sf.safe_fetch(url, method=method, transport=transport, **kwargs)

    monkeypatch.setattr(dry_check, "safe_fetch", _patched_safe_fetch)

    report = await run_dry_check("https://api.example.com/free")

    assert report.http_status == 200
    assert report.checks == []
    assert "200" in report.verdict


async def test_dry_check_handles_invalid_json(fake_resolver, monkeypatch):
    import dry_check

    def handler(request: httpx.Request) -> httpx.Response:
        return _streaming_response(402, b"<html>not json</html>")

    transport = httpx.MockTransport(handler)

    async def _patched_safe_fetch(url, method="GET", **kwargs):
        return await sf.safe_fetch(url, method=method, transport=transport, **kwargs)

    monkeypatch.setattr(dry_check, "safe_fetch", _patched_safe_fetch)

    report = await run_dry_check("https://api.example.com/broken")

    assert report.http_status == 402
    assert report.parse_error is not None
    assert report.checks == []


async def test_dry_check_omits_bazaar_check_when_no_client_given(fake_resolver, monkeypatch):
    """The default, and every other test in this file: no bazaar_client
    means no bazaar_index_status entry at all, not a SKIP placeholder --
    see bazaar.check_bazaar_index_status's docstring for why."""
    import dry_check

    def handler(request: httpx.Request) -> httpx.Response:
        return _streaming_response(402, json.dumps(GOOD_CHALLENGE).encode())

    transport = httpx.MockTransport(handler)

    async def _patched_safe_fetch(url, method="GET", **kwargs):
        return await sf.safe_fetch(url, method=method, transport=transport, **kwargs)

    monkeypatch.setattr(dry_check, "safe_fetch", _patched_safe_fetch)

    report = await run_dry_check("https://api.example.com/data")

    assert "bazaar_index_status" not in {c.check_id for c in report.checks}


async def test_dry_check_includes_bazaar_check_when_client_given(fake_resolver, monkeypatch):
    import dry_check

    def handler(request: httpx.Request) -> httpx.Response:
        return _streaming_response(402, json.dumps(GOOD_CHALLENGE).encode())

    transport = httpx.MockTransport(handler)

    async def _patched_safe_fetch(url, method="GET", **kwargs):
        return await sf.safe_fetch(url, method=method, transport=transport, **kwargs)

    monkeypatch.setattr(dry_check, "safe_fetch", _patched_safe_fetch)

    bazaar_client = FakeBazaarClient(response=make_response(active=True))

    report = await run_dry_check("https://api.example.com/data", bazaar_client=bazaar_client)

    statuses = {c.check_id: c.status for c in report.checks}
    assert statuses["bazaar_index_status"] == Status.PASS
    assert len(bazaar_client.calls) == 1
