"""
bazaar.lookup_curated: after the index check, look the target up in CDP's
discovery listing by its payTo and report `curated` (true, false, or null
for unknown) in the top-level `bazaar` summary. A failed lookup must never
fail the diagnosis.
"""

import asyncio
import copy
import json

import httpx
import pytest
from fastapi.testclient import TestClient

import bazaar
import dry_check
import main
import safe_fetch as sf
from diagnosis import CheckResult, Status
from tests.test_bazaar_raw import FakeRawResponse
from tests.test_dry_check import GOOD_CHALLENGE, _streaming_response, fake_resolver  # noqa: F401

PAY_TO = GOOD_CHALLENGE["accepts"][0]["payTo"]
TARGET = "https://api.seller.test/sentiment/BTC"
TEMPLATE = "https://api.seller.test/sentiment/:symbol"


def _challenge(resource_url=TARGET):
    challenge = copy.deepcopy(GOOD_CHALLENGE)
    challenge["resource"]["url"] = resource_url
    return challenge


def _listing(*resources, total=None):
    return {
        "pagination": {"limit": 100, "offset": 0, "total": len(resources) if total is None else total},
        "payTo": PAY_TO,
        "resources": list(resources),
    }


class FakeCdp:
    """Raw-JSON CDP client: validate (for the index check) plus merchant pages."""

    def __init__(self, pages=None, *, active=True, merchant_error=None):
        self.pages = pages or [_listing()]
        self.active = active
        self.merchant_error = merchant_error
        self.merchant_calls = []

    async def validate_x402_resource_without_preload_content(self, request):
        return FakeRawResponse({
            "valid": True, "statusCode": 402, "x402Version": 2, "preflight": [],
            "simulation": {"outcome": "accepted"},
            "index": {"active": self.active, "lastCrawledAt": "2026-10-01T00:00:00Z"} if self.active else None,
        })

    async def list_x402_discovery_merchant_without_preload_content(self, pay_to, limit, offset):
        self.merchant_calls.append((pay_to, limit, offset))
        if self.merchant_error is not None:
            raise self.merchant_error
        page = offset // limit
        return FakeRawResponse(self.pages[page] if page < len(self.pages) else _listing())


def _index(status):
    return CheckResult(check_id=bazaar.CHECK_ID, status=status, detail="x")


@pytest.mark.parametrize(
    "listed, expected",
    [
        ({"resource": TEMPLATE, "curated": True}, True),   # CDP lists route templates
        ({"resource": TEMPLATE}, False),                    # field omitted = not curated
        ({"resource": TARGET, "curated": True}, True),      # exact URL
        ({"resource": TARGET + "/", "curated": False}, False),  # trailing slash ignored
    ],
)
async def test_lookup_reads_curated_flag(listed, expected):
    cdp = FakeCdp([_listing({"resource": "https://other.test/x", "curated": True}, listed)])
    assert await bazaar.lookup_curated(_challenge(), TARGET, cdp, _index(Status.PASS)) is expected
    assert cdp.merchant_calls[0][0] == PAY_TO


async def test_exact_match_wins_over_template():
    cdp = FakeCdp([_listing({"resource": TEMPLATE, "curated": True}, {"resource": TARGET})])
    assert await bazaar.lookup_curated(_challenge(), TARGET, cdp, _index(Status.PASS)) is False


async def test_finds_resource_on_a_later_page():
    first = _listing(*({"resource": f"https://api.seller.test/r{i}"} for i in range(100)), total=101)
    second = _listing({"resource": TEMPLATE, "curated": True}, total=101)
    cdp = FakeCdp([first, second])
    assert await bazaar.lookup_curated(_challenge(), TARGET, cdp, _index(Status.PASS)) is True
    assert [offset for _, _, offset in cdp.merchant_calls] == [0, 100]


@pytest.mark.parametrize(
    "index_status, expected",
    [(Status.FAIL, False), (Status.WARN, False), (Status.PASS, None), (Status.SKIP, None)],
)
async def test_unmatched_resource(index_status, expected):
    # Not in the listing: only "not indexed" (per CDP's validate) can mean not curated.
    cdp = FakeCdp([_listing({"resource": "https://api.seller.test/other/path"})])
    assert await bazaar.lookup_curated(_challenge(), TARGET, cdp, _index(index_status)) is expected


async def test_unknown_without_client_or_payto():
    assert await bazaar.lookup_curated(_challenge(), TARGET, None, _index(Status.PASS)) is None
    no_pay_to = _challenge()
    del no_pay_to["accepts"][0]["payTo"]
    assert await bazaar.lookup_curated(no_pay_to, TARGET, FakeCdp(), _index(Status.PASS)) is None


async def test_failures_are_unknown(monkeypatch):
    challenge, index = _challenge(), _index(Status.FAIL)
    assert await bazaar.lookup_curated(challenge, TARGET, FakeCdp(merchant_error=RuntimeError("boom")), index) is None
    http_500 = FakeCdp()
    http_500.pages = None

    async def server_error(pay_to, limit, offset):
        return FakeRawResponse({"error": "x"}, status=500, reason="Server Error")

    http_500.list_x402_discovery_merchant_without_preload_content = server_error
    assert await bazaar.lookup_curated(challenge, TARGET, http_500, index) is None

    slow = FakeCdp()

    async def hang(pay_to, limit, offset):
        await asyncio.sleep(5)

    slow.list_x402_discovery_merchant_without_preload_content = hang
    monkeypatch.setattr(bazaar, "CURATED_LOOKUP_TIMEOUT_SECONDS", 0.05)
    assert await bazaar.lookup_curated(challenge, TARGET, slow, index) is None


# --- through POST /diagnose ------------------------------------------------------


@pytest.fixture
def target_402(fake_resolver, monkeypatch):  # noqa: F811
    transport = httpx.MockTransport(
        lambda request: _streaming_response(402, json.dumps(_challenge()).encode())
    )

    async def patched(url, method="GET", **kwargs):
        return await sf.safe_fetch(url, method=method, transport=transport, **kwargs)

    monkeypatch.setattr(dry_check, "safe_fetch", patched)


def _diagnose(cdp):
    client = TestClient(main.create_app(bazaar_client=cdp))
    resp = client.post("/diagnose", json={"url": TARGET})
    assert resp.status_code == 200
    return resp.json()


def test_diagnose_reports_curated_next_to_index_status(target_402):
    body = _diagnose(FakeCdp([_listing({"resource": TEMPLATE, "curated": True})]))
    assert body["bazaar"]["indexed"] is True
    assert body["bazaar"]["curated"] is True
    assert list(body["bazaar"]) == ["indexed", "status", "curated", "detail"]
    assert "curated" not in body  # only in the summary, not duplicated at the top level


def test_failed_lookup_never_fails_the_diagnosis(target_402):
    body = _diagnose(FakeCdp(merchant_error=httpx.ConnectError("CDP unreachable")))
    assert body["bazaar"]["indexed"] is True
    assert body["bazaar"]["curated"] is None
    assert body["checks"]


def test_bazaar_extension_output_example_shows_curated():
    import payment

    example = payment.BAZAAR_EXTENSION["bazaar"]["info"]["output"]["example"]
    assert example["bazaar"]["curated"] is False
