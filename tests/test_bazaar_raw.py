"""
bazaar.py reads CDP's validate response as raw JSON (see _validate_raw).
Regression for 2026-09-29: CDP returned a new preflight check name,
"url_valid", which cdp-sdk 1.48.1's response model rejects, so every live
index lookup came back SKIP ("indexed": null) in production.
"""

import json

import pytest

from bazaar import CHECK_ID, check_bazaar_index_status, summarize_index_status
from diagnosis import Status

URL = "https://crypto-sentiment-x402.onrender.com/sentiment/BTC"


class FakeRawResponse:
    def __init__(self, payload, status=200, reason="OK"):
        self._body = json.dumps(payload).encode() if not isinstance(payload, bytes) else payload
        self.status = status
        self.reason = reason

    async def read(self):
        return self._body


class FakeRawClient:
    def __init__(self, response):
        self._response = response
        self.calls = []

    async def validate_x402_resource_without_preload_content(self, request):
        self.calls.append(request)
        return self._response

    async def validate_x402_resource(self, request):  # must NOT be used
        raise AssertionError("parsed SDK path used instead of raw JSON")


def _payload(*, active, preflight, outcome=None, rejection=None):
    return {
        "valid": True,
        "statusCode": 402,
        "x402Version": 2,
        "preflight": preflight,
        "simulation": {"outcome": outcome, "rejectionReason": rejection} if outcome else None,
        "index": {
            "active": active,
            "lastCrawledAt": "2026-09-29T10:00:00Z" if active else None,
            "quality": {"l30DaysTotalCalls": 7, "l30DaysUniquePayers": 2, "lastCalledAt": None} if active else None,
        },
    }


URL_VALID_OK = {"check": "url_valid", "passed": True, "detail": None, "severity": "required"}


def test_sdk_model_really_rejects_url_valid():
    """Documents the upstream bug this module works around."""
    from cdp.openapi_client.models.x402_validate_response import X402ValidateResponse

    with pytest.raises(Exception):
        X402ValidateResponse.from_json(json.dumps(_payload(active=True, preflight=[URL_VALID_OK])))


async def test_indexed_with_unknown_check_name_passes():
    client = FakeRawClient(FakeRawResponse(_payload(active=True, preflight=[URL_VALID_OK])))
    result = await check_bazaar_index_status(URL, client)

    assert result.check_id == CHECK_ID
    assert result.status == Status.PASS
    assert "Last crawled 2026-09-29T10:00:00Z" in result.detail
    assert "7 call(s) / 2 unique payer(s)" in result.detail
    assert summarize_index_status([result])["indexed"] is True
    assert len(client.calls) == 1


async def test_failing_unknown_check_is_reported_by_name():
    bad = {"check": "url_valid", "passed": False, "detail": "URL has a query string", "severity": "required"}
    client = FakeRawClient(FakeRawResponse(
        _payload(active=False, preflight=[bad], outcome="rejected", rejection="invalid url")))
    result = await check_bazaar_index_status(URL, client)

    assert result.status == Status.FAIL
    assert "url_valid: URL has a query string" in result.detail
    assert "invalid url" in result.detail
    assert summarize_index_status([result])["indexed"] is False


async def test_would_be_accepted_maps_to_warn():
    client = FakeRawClient(FakeRawResponse(_payload(active=False, preflight=[URL_VALID_OK], outcome="accepted")))
    result = await check_bazaar_index_status(URL, client)
    assert result.status == Status.WARN
    assert summarize_index_status([result])["status"] == "not_indexed_would_be_accepted"


async def test_cdp_http_error_skips_cleanly():
    client = FakeRawClient(FakeRawResponse(b'{"errorMessage":"down"}', status=503, reason="Service Unavailable"))
    result = await check_bazaar_index_status(URL, client)
    assert result.status == Status.SKIP
    assert "HTTP 503" in result.detail


async def test_non_json_body_skips_cleanly():
    client = FakeRawClient(FakeRawResponse(b"<html>gateway</html>"))
    result = await check_bazaar_index_status(URL, client)
    assert result.status == Status.SKIP
