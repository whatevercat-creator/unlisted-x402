"""
Tests for bazaar.py's check_bazaar_index_status -- build-order step 9.

Uses simple duck-typed fakes (types.SimpleNamespace) for CDP's response
objects rather than constructing real cdp.openapi_client pydantic response
models: _result_from_validate_response only ever reads attributes via
getattr() (deliberately, so a shape CDP adds fields to later doesn't break
this module), so a fake with just the attributes this module actually
reads exercises the real branching logic without coupling these tests to
every required field those generated models happen to declare today. The
real ApiException class *is* used for the CDP-error path, since that's
what bazaar.py's except clause actually matches on.
"""

from types import SimpleNamespace

import pytest

from bazaar import CHECK_ID, check_bazaar_index_status
from diagnosis import Confidence, Status


class FakeBazaarClient:
    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc
        self.calls = []

    async def validate_x402_resource(self, request):
        self.calls.append(request)
        if self._exc is not None:
            raise self._exc
        return self._response


def make_response(*, active=False, outcome=None, rejection_reason=None, preflight=None, quality=None):
    index = SimpleNamespace(active=active, last_crawled_at="2026-09-23T12:00:00Z" if active else None, quality=quality)
    simulation = SimpleNamespace(outcome=outcome, rejection_reason=rejection_reason) if outcome else None
    return SimpleNamespace(
        valid=True,
        status_code=402,
        x402_version=2,
        preflight=preflight or [],
        payment_requirements=None,
        bazaar_extension=None,
        simulation=simulation,
        index=index,
    )


URL = "https://target.example.com/sentiment/BTC"


async def test_returns_none_when_no_client_configured():
    result = await check_bazaar_index_status(URL, None)
    assert result is None


async def test_skips_cleanly_for_non_https_url_without_touching_client():
    """Confirmed empirically: CDP's real X402ValidateRequest rejects a
    `resource` that doesn't match ^https://.*$ with a pydantic
    ValidationError. Checked explicitly up front so the SKIP reason is a
    clean sentence, and so a non-https target never even reaches the
    client (see the client's call count assertion below)."""
    client = FakeBazaarClient(response=make_response(active=True))

    result = await check_bazaar_index_status("http://target.example.com/sentiment/BTC", client)

    assert result.status == Status.SKIP
    assert "https" in result.detail.lower()
    assert client.calls == []


async def test_indexed_resource_passes_with_quality_metrics():
    quality = SimpleNamespace(l30_days_total_calls=4, l30_days_unique_payers=1, last_called_at="2026-09-24T00:00:00Z")
    response = make_response(active=True, quality=quality)
    client = FakeBazaarClient(response=response)

    result = await check_bazaar_index_status(URL, client)

    assert result.check_id == CHECK_ID
    assert result.status == Status.PASS
    assert result.confidence == Confidence.FACILITATOR
    assert "Currently indexed" in result.detail
    assert "4 call(s) / 1 unique payer(s)" in result.detail
    assert client.calls[0].resource == URL
    assert client.calls[0].method == "GET"


async def test_not_indexed_but_would_be_accepted_warns_about_crawl_lag():
    response = make_response(active=False, outcome="accepted")
    client = FakeBazaarClient(response=response)

    result = await check_bazaar_index_status(URL, client)

    assert result.status == Status.WARN
    assert "crawl lag" in result.detail.lower()
    assert result.fix is not None


async def test_not_indexed_and_rejected_fails_with_reason():
    response = make_response(
        active=False,
        outcome="rejected",
        rejection_reason="missing extensions.bazaar",
        preflight=[
            SimpleNamespace(check="hasBazaarExtension", passed=False, detail="extensions.bazaar missing"),
            SimpleNamespace(check="reachable", passed=True, detail="ok"),
        ],
    )
    client = FakeBazaarClient(response=response)

    result = await check_bazaar_index_status(URL, client)

    assert result.status == Status.FAIL
    assert result.confidence == Confidence.FACILITATOR
    assert "missing extensions.bazaar" in result.detail
    assert "hasBazaarExtension" in result.detail


async def test_not_indexed_with_no_simulation_at_all_still_fails_gracefully():
    """Defensive path: some future/unexpected response shape with no
    simulation object at all shouldn't crash -- just falls to FAIL with
    whatever detail is available."""
    response = make_response(active=False, outcome=None)
    client = FakeBazaarClient(response=response)

    result = await check_bazaar_index_status(URL, client)

    assert result.status == Status.FAIL


async def test_api_exception_from_cdp_skips_rather_than_raises():
    from cdp.openapi_client.exceptions import ApiException

    client = FakeBazaarClient(exc=ApiException(status=503, reason="Service Unavailable"))

    result = await check_bazaar_index_status(URL, client)

    assert result.status == Status.SKIP
    assert result.confidence == Confidence.CLIENT
    assert "503" in result.detail


async def test_unexpected_network_error_from_cdp_skips_rather_than_raises():
    client = FakeBazaarClient(exc=TimeoutError("connect timed out"))

    result = await check_bazaar_index_status(URL, client)

    assert result.status == Status.SKIP
    assert "connect timed out" in result.detail
