"""
payment_required_header: an x402 v2 402 must carry its challenge in the
PAYMENT-REQUIRED header. CDP's validator and the v2 Bazaar indexer read
the header, while decode_challenge falls back to the body, so without this
check a body-only v2 route passed every other check.

Local fixtures only: no network, no payments.
"""

import base64
import json

import httpx

import dry_check
import safe_fetch as sf
from diagnosis import Status, check_payment_required_header
from tests.test_dry_check import GOOD_CHALLENGE, _streaming_response, _v2_402, fake_resolver  # noqa: F401

V1_CHALLENGE = {
    "x402Version": 1,
    "error": "X-PAYMENT header is required",
    "accepts": [
        {
            "scheme": "exact",
            "network": "base",
            "maxAmountRequired": "10000",
            "resource": "https://api.example.com/data",
            "description": "Sentiment data for a given token.",
            "mimeType": "application/json",
            "payTo": "0xA6E10884a70D00DE8b8c5dfF0a2a9B1a821cE074",
            "maxTimeoutSeconds": 300,
            "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "extra": {"name": "USD Coin", "version": "2"},
        }
    ],
}


def _b64(challenge) -> str:
    return base64.b64encode(json.dumps(challenge).encode()).decode()


def _body(challenge) -> bytes:
    return json.dumps(challenge).encode()


# --------------------------------------------------------------------------
# The check itself
# --------------------------------------------------------------------------


def test_header_present_and_valid_passes():
    result = check_payment_required_header(_b64(GOOD_CHALLENGE), _body(GOOD_CHALLENGE))
    assert result.status == Status.PASS
    assert result.fix is None


def test_header_valid_passes_even_with_empty_body():
    # The confirmed real shape: header only, body `{}`.
    assert check_payment_required_header(_b64(GOOD_CHALLENGE), b"{}").status == Status.PASS


def test_unpadded_header_passes():
    # decode_challenge accepts unpadded base64; the check must agree with it.
    header = _b64(GOOD_CHALLENGE).rstrip("=")
    assert check_payment_required_header(header, b"{}").status == Status.PASS


def test_v2_body_only_fails_with_plain_fix():
    result = check_payment_required_header(None, _body(GOOD_CHALLENGE))
    assert result.status == Status.FAIL
    assert result.fix == (
        "Your 402 body is fine, but the PAYMENT-REQUIRED header is missing. For x402 v2, "
        "CDP's validator and the Bazaar indexer read the header, so the route can't be "
        "listed. Send the same JSON, base64-encoded, in a PAYMENT-REQUIRED header."
    )


def test_v2_no_header_and_no_payment_options_fails_with_its_own_message():
    result = check_payment_required_header(None, b'{"x402Version": 2, "accepts": []}')
    assert result.status == Status.FAIL
    assert "Your 402 body is fine" not in result.fix


def test_malformed_header_fails():
    result = check_payment_required_header("not base64 json!!", _body(GOOD_CHALLENGE))
    assert result.status == Status.FAIL
    assert "doesn't decode" in result.detail
    assert "Your 402 body is fine" not in result.fix


def test_header_decoding_to_json_without_payment_options_fails():
    for value in ({"x402Version": 2}, {"x402Version": 2, "accepts": []}, [1, 2]):
        result = check_payment_required_header(_b64(value), _body(GOOD_CHALLENGE))
        assert result.status == Status.FAIL, value


def test_empty_header_fails_as_malformed():
    assert check_payment_required_header("", _body(GOOD_CHALLENGE)).status == Status.FAIL


def test_v1_body_without_header_is_skipped():
    result = check_payment_required_header(None, _body(V1_CHALLENGE))
    assert result.status == Status.SKIP
    assert "x402Version 1" in result.detail


def test_unknown_version_without_header_is_skipped():
    challenge = {k: v for k, v in GOOD_CHALLENGE.items() if k != "x402Version"}
    assert check_payment_required_header(None, _body(challenge)).status == Status.SKIP
    assert check_payment_required_header(None, b"<html>").status == Status.SKIP


# --------------------------------------------------------------------------
# End to end through run_dry_check
# --------------------------------------------------------------------------


def _patch(monkeypatch, response_factory):
    transport = httpx.MockTransport(lambda request: response_factory())

    async def _patched_safe_fetch(url, method="GET", **kwargs):
        return await sf.safe_fetch(url, method=method, transport=transport, **kwargs)

    monkeypatch.setattr(dry_check, "safe_fetch", _patched_safe_fetch)


async def test_dry_check_passes_v2_route_with_header(fake_resolver, monkeypatch):
    _patch(monkeypatch, lambda: _v2_402(GOOD_CHALLENGE))
    report = await dry_check.run_dry_check("https://api.example.com/data")
    statuses = {c.check_id: c.status for c in report.checks}
    assert statuses["payment_required_header"] == Status.PASS
    assert "No issues found" in report.verdict


async def test_dry_check_fails_v2_route_with_body_only(fake_resolver, monkeypatch):
    """The reported route: a correct v2 body, no PAYMENT-REQUIRED header.
    Every other check passes, so this is the only failure."""
    _patch(monkeypatch, lambda: _streaming_response(402, _body(GOOD_CHALLENGE)))
    report = await dry_check.run_dry_check("https://api.example.com/data")
    assert [c.check_id for c in report.failures] == ["payment_required_header"]
    assert report.verdict == "1 issue(s) found: payment_required_header"


async def test_dry_check_flags_malformed_header_but_still_checks_body(fake_resolver, monkeypatch):
    _patch(
        monkeypatch,
        lambda: _streaming_response(
            402, _body(GOOD_CHALLENGE), headers={"payment-required": "%%%not-base64%%%"}
        ),
    )
    report = await dry_check.run_dry_check("https://api.example.com/data")
    statuses = {c.check_id: c.status for c in report.checks}
    assert statuses["payment_required_header"] == Status.FAIL
    # decode_challenge still falls back to the body for the other checks.
    assert statuses["bazaar_extension"] == Status.PASS


async def test_dry_check_skips_v1_body_only_route(fake_resolver, monkeypatch):
    _patch(monkeypatch, lambda: _streaming_response(402, _body(V1_CHALLENGE)))
    report = await dry_check.run_dry_check("https://api.example.com/data")
    statuses = {c.check_id: c.status for c in report.checks}
    assert statuses["payment_required_header"] == Status.SKIP
    assert "payment_required_header" not in {c.check_id for c in report.failures}
