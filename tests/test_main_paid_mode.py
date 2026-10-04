"""
Tests for main.py's `?mode=paid` wiring: pricing, availability gating, and
the per-target-domain daily limit on real payment tests.

Builds on the same FakeFacilitatorClient (inbound side, from test_payment.py)
plus a fake target app + throwaway signer (outbound side, from
test_paid_check.py / test_outbound_payment.py) to exercise the whole request
through main.create_app() rather than testing paid_check.py or
outbound_payment.py in isolation again.
"""

import base64
import json

import httpx
import pytest
from eth_account import Account
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
import outbound_payment
import payment
import safe_fetch as sf
from limits import EconomicCeiling, SlidingWindowRateLimiter
from safe_fetch import ResolvedTarget
from tests.test_payment import FakeFacilitatorClient, PAY_TO

from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http.types import PaymentOption, RouteConfig
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.schemas import SettleResponse, SupportedKind, SupportedResponse, VerifyResponse
from x402.server import x402ResourceServer

SIGNER = Account.create()
TARGET_URL = "https://target.seller.test/sentiment/BTC"


class FakeSellerFacilitator:
    def __init__(self):
        self.settle_calls = []

    def get_supported(self):
        return SupportedResponse(
            kinds=[
                SupportedKind(
                    x402_version=2, scheme=outbound_payment.SCHEME, network=outbound_payment.NETWORK
                )
            ]
        )

    async def verify(self, payload, requirements):
        return VerifyResponse(is_valid=True, payer=SIGNER.address)

    async def settle(self, payload, requirements):
        self.settle_calls.append((payload, requirements))
        return SettleResponse(
            success=True, transaction="0xdeadbeef", network=requirements.network, payer=SIGNER.address
        )


def make_target_app(price="$0.01"):
    app = FastAPI()

    @app.get("/sentiment/BTC")
    async def resource():
        return {"sentiment": "bullish"}

    fake = FakeSellerFacilitator()
    server = x402ResourceServer(fake)
    server.register(outbound_payment.NETWORK, ExactEvmServerScheme())
    routes = {
        "GET /sentiment/BTC": RouteConfig(
            accepts=PaymentOption(
                scheme=outbound_payment.SCHEME,
                pay_to="0x" + "9" * 40,
                price=price,
                network=outbound_payment.NETWORK,
            ),
        )
    }
    app.add_middleware(PaymentMiddlewareASGI, routes=routes, server=server)
    return app, fake


@pytest.fixture(autouse=True)
def fake_resolver_and_limiters(monkeypatch):
    async def _fake(hostname, port):
        return ResolvedTarget(hostname=hostname, port=port, ip="1.2.3.4", family=2)

    monkeypatch.setattr(sf, "resolve_and_validate", _fake)

    main.caller_limiter = SlidingWindowRateLimiter(
        limit=main.CALLER_LIMIT, window_seconds=main.CALLER_WINDOW_SECONDS
    )
    main.domain_limiter = SlidingWindowRateLimiter(
        limit=main.DOMAIN_LIMIT, window_seconds=main.DOMAIN_WINDOW_SECONDS
    )
    main.paid_test_domain_limiter = SlidingWindowRateLimiter(
        limit=main.PAID_TEST_DOMAIN_LIMIT, window_seconds=main.PAID_TEST_DOMAIN_WINDOW_SECONDS
    )
    yield


def _decode_challenge(response) -> dict:
    header = response.headers["payment-required"]
    return json.loads(base64.b64decode(header))


def _payment_signature_header_for(challenge: dict, payer=None) -> str:
    requirements = challenge["accepts"][0]
    payload = {
        "x402Version": 2,
        "payload": {"signature": "0xfake", "authorization": {"from": payer or "0x" + "1" * 40}},
        "accepted": requirements,
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


def _paid_ready_app(target_app, economic_ceiling=None):
    return main.create_app(
        facilitator_client=FakeFacilitatorClient(),
        pay_to=PAY_TO,
        outbound_signer=SIGNER,
        outbound_transport=httpx.ASGITransport(app=target_app),
    )


def test_mode_paid_costs_more_than_dry_mode():
    target_app, _ = make_target_app()
    app = _paid_ready_app(target_app)
    client = TestClient(app)

    dry = _decode_challenge(client.post("/diagnose", json={"url": TARGET_URL}))
    paid = _decode_challenge(client.post("/diagnose?mode=paid", json={"url": TARGET_URL}))

    dry_amount = int(dry["accepts"][0]["amount"])
    paid_amount = int(paid["accepts"][0]["amount"])
    assert paid_amount > dry_amount


def test_mode_paid_rejected_with_503_when_no_outbound_wallet_configured():
    # Paywall active, but no outbound_signer / outbound_wallet_name given.
    app = main.create_app(facilitator_client=FakeFacilitatorClient(), pay_to=PAY_TO)
    client = TestClient(app)

    challenge = _decode_challenge(client.post("/diagnose?mode=paid", json={"url": TARGET_URL}))
    header = _payment_signature_header_for(challenge)

    resp = client.post(
        "/diagnose?mode=paid", json={"url": TARGET_URL}, headers={"PAYMENT-SIGNATURE": header}
    )
    assert resp.status_code == 503


def test_mode_paid_rejected_when_paywall_not_active_even_if_outbound_configured():
    """The important safety property: an outbound wallet configured without
    an active inbound paywall must never make real payment testing free."""
    target_app, fake_seller = make_target_app()
    app = main.create_app(
        outbound_signer=SIGNER,
        outbound_transport=httpx.ASGITransport(app=target_app),
    )
    client = TestClient(app)

    resp = client.post("/diagnose?mode=paid", json={"url": TARGET_URL})
    assert resp.status_code == 503
    assert len(fake_seller.settle_calls) == 0


def test_mode_paid_succeeds_and_returns_settlement_echo_check():
    target_app, fake_seller = make_target_app()
    app = _paid_ready_app(target_app)
    client = TestClient(app)

    challenge = _decode_challenge(client.post("/diagnose?mode=paid", json={"url": TARGET_URL}))
    header = _payment_signature_header_for(challenge)

    resp = client.post(
        "/diagnose?mode=paid", json={"url": TARGET_URL}, headers={"PAYMENT-SIGNATURE": header}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "paid"
    settlement = [c for c in body["checks"] if c["check_id"] == "settlement_echo"][0]
    assert settlement["status"] == "pass"
    assert len(fake_seller.settle_calls) == 1


def test_mode_paid_second_test_against_same_domain_within_24h_is_rate_limited():
    target_app, fake_seller = make_target_app()
    app = _paid_ready_app(target_app)
    client = TestClient(app)

    challenge = _decode_challenge(client.post("/diagnose?mode=paid", json={"url": TARGET_URL}))
    header = _payment_signature_header_for(challenge)

    first = client.post(
        "/diagnose?mode=paid", json={"url": TARGET_URL}, headers={"PAYMENT-SIGNATURE": header}
    )
    assert first.status_code == 200

    # Second paid-mode request against the SAME domain -- get a fresh
    # challenge (each POST issues its own), pay it, and expect the
    # per-target-24h gate to block it before any of our own payment is
    # verified/settled.
    challenge2 = _decode_challenge(client.post("/diagnose?mode=paid", json={"url": TARGET_URL}))
    header2 = _payment_signature_header_for(challenge2)
    second = client.post(
        "/diagnose?mode=paid", json={"url": TARGET_URL}, headers={"PAYMENT-SIGNATURE": header2}
    )
    assert second.status_code == 429
    assert len(fake_seller.settle_calls) == 1  # still just the first request's settle


def test_dry_mode_against_same_domain_is_unaffected_by_paid_test_limit(monkeypatch):
    target_app, fake_seller = make_target_app()
    app = _paid_ready_app(target_app)
    client = TestClient(app)

    challenge = _decode_challenge(client.post("/diagnose?mode=paid", json={"url": TARGET_URL}))
    header = _payment_signature_header_for(challenge)
    paid = client.post(
        "/diagnose?mode=paid", json={"url": TARGET_URL}, headers={"PAYMENT-SIGNATURE": header}
    )
    assert paid.status_code == 200

    # Dry mode doesn't go through outbound_transport (only paid mode does --
    # dry_check.run_dry_check always uses real network), so it's monkeypatched
    # here purely to avoid a real network call in this sandbox; the point of
    # this test is that main.paid_test_domain_limiter (1/day) is never even
    # consulted for a dry-mode request, regardless of how it fetches.
    from diagnosis import DiagnosisReport

    async def _fake_dry_report(url: str):
        return DiagnosisReport(url=url, mode="dry", http_status=402, checks=[], verdict="ok")

    monkeypatch.setattr(main, "run_dry_check", _fake_dry_report)

    dry_challenge = _decode_challenge(client.post("/diagnose", json={"url": TARGET_URL}))
    dry_header = _payment_signature_header_for(dry_challenge)
    dry = client.post("/diagnose", json={"url": TARGET_URL}, headers={"PAYMENT-SIGNATURE": dry_header})
    assert dry.status_code == 200
    assert dry.json()["mode"] == "dry"


def _target_answering_400():
    app = FastAPI()

    @app.get("/sentiment/BTC")
    async def resource():
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=400, content={"error": "missing window"})

    return app


def test_mode_paid_non_402_target_is_not_charged_and_keeps_the_paid_test_slot():
    inbound = FakeFacilitatorClient()
    app = main.create_app(
        facilitator_client=inbound,
        pay_to=PAY_TO,
        outbound_signer=SIGNER,
        outbound_transport=httpx.ASGITransport(app=_target_answering_400()),
    )
    client = TestClient(app)

    for _ in range(2):  # a repeat within 24h isn't blocked: no slot was used
        challenge = _decode_challenge(client.post("/diagnose?mode=paid", json={"url": TARGET_URL}))
        resp = client.post(
            "/diagnose?mode=paid",
            json={"url": TARGET_URL},
            headers={"PAYMENT-SIGNATURE": _payment_signature_header_for(challenge)},
        )
        assert resp.status_code == 422
        body = resp.json()
        assert body["mode"] == "paid"
        assert body["http_status"] == 400
        assert body["checks"] == []
        assert "validation is most likely running before the paywall" in body["verdict"]
        assert body["verdict"].endswith(
            f"You were not charged. Once the URL returns a 402, run the "
            f"{payment.DEFAULT_PRICE_USD} Check for a full diagnosis."
        )
        assert "payment-response" not in resp.headers

    assert len(inbound.verify_calls) == 2
    assert len(inbound.settle_calls) == 0
    assert main.paid_test_domain_limiter.current_count("target.seller.test") == 0


def test_mode_paid_402_target_still_charged_and_uses_the_slot():
    target_app, _ = make_target_app()
    inbound = FakeFacilitatorClient()
    app = main.create_app(
        facilitator_client=inbound,
        pay_to=PAY_TO,
        outbound_signer=SIGNER,
        outbound_transport=httpx.ASGITransport(app=target_app),
    )
    client = TestClient(app)
    challenge = _decode_challenge(client.post("/diagnose?mode=paid", json={"url": TARGET_URL}))
    resp = client.post(
        "/diagnose?mode=paid",
        json={"url": TARGET_URL},
        headers={"PAYMENT-SIGNATURE": _payment_signature_header_for(challenge)},
    )
    assert resp.status_code == 200
    assert "not charged" not in resp.json()["verdict"]
    assert len(inbound.settle_calls) == 1
    assert main.paid_test_domain_limiter.current_count("target.seller.test") == 1


def test_openapi_documents_the_unpaid_422():
    op = TestClient(main.create_app()).get("/openapi.json").json()["paths"]["/diagnose"]["post"]
    assert "you are not charged" in op["responses"]["422"]["description"]
    assert "422" in op["description"]


@pytest.mark.parametrize(
    "error, status",
    [(sf.FetchError("connection refused"), 502), (sf.SSRFBlocked("private address"), 400)],
)
def test_mode_paid_unreachable_or_blocked_target_keeps_the_paid_test_slot(monkeypatch, error, status):
    inbound = FakeFacilitatorClient()
    app = main.create_app(
        facilitator_client=inbound,
        pay_to=PAY_TO,
        outbound_signer=SIGNER,
        outbound_transport=httpx.ASGITransport(app=make_target_app()[0]),
    )

    async def _raise(*args, **kwargs):
        raise error

    monkeypatch.setattr(main, "run_paid_check", _raise)
    client = TestClient(app)

    for _ in range(2):  # a repeat within 24h isn't blocked: no slot was used
        challenge = _decode_challenge(client.post("/diagnose?mode=paid", json={"url": TARGET_URL}))
        resp = client.post(
            "/diagnose?mode=paid",
            json={"url": TARGET_URL},
            headers={"PAYMENT-SIGNATURE": _payment_signature_header_for(challenge)},
        )
        assert resp.status_code == status

    assert len(inbound.settle_calls) == 0
    assert main.paid_test_domain_limiter.current_count("target.seller.test") == 0


# --------------------------------------------------------------------------
# 402 but no test payment: 422 with only the reason, not charged, slot kept
# --------------------------------------------------------------------------


def _scripted_target(*steps):
    """httpx transport answering request N with steps[N] (the last step
    repeats). A step is a challenge dict (served as a 402, header and
    body), "garbage402", "ok", or an exception to raise."""
    from tests.test_dry_check import GOOD_CHALLENGE, _streaming_response

    seen = []

    def handler(request):
        step = steps[min(len(seen), len(steps) - 1)]
        seen.append(request)
        if isinstance(step, Exception):
            raise step
        if step == "ok":
            return _streaming_response(200, b'{"sentiment": "bullish"}')
        if step == "garbage402":
            return _streaming_response(402, b"<html>pay me</html>")
        body = json.dumps(step).encode()
        return _streaming_response(
            402, body, headers={"payment-required": base64.b64encode(body).decode()}
        )

    return httpx.MockTransport(handler), seen


def _challenge(**accept_overrides):
    import copy

    from tests.test_dry_check import GOOD_CHALLENGE

    challenge = copy.deepcopy(GOOD_CHALLENGE)
    challenge["resource"]["url"] = TARGET_URL
    del challenge["extensions"]  # no example input, so no probe request
    challenge["accepts"][0].update(accept_overrides)
    return challenge


def _paid_call(transport, inbound=None):
    inbound = inbound or FakeFacilitatorClient()
    app = main.create_app(
        facilitator_client=inbound, pay_to=PAY_TO, outbound_signer=SIGNER, outbound_transport=transport
    )
    client = TestClient(app)
    challenge = _decode_challenge(client.post("/diagnose?mode=paid", json={"url": TARGET_URL}))
    resp = client.post(
        "/diagnose?mode=paid",
        json={"url": TARGET_URL},
        headers={"PAYMENT-SIGNATURE": _payment_signature_header_for(challenge)},
    )
    return resp, inbound


NOT_CHARGED = "You were not charged. Run the $0.02 Check for the diagnosis."


@pytest.mark.parametrize(
    "steps, reason",
    [
        (["garbage402"], "couldn't be parsed as an x402 challenge"),
        ([_challenge(network="eip155:1")], "None of the target's payment options can be paid"),
        ([_challenge(extra={"name": "Mystery Token", "version": "1"})], "couldn't be read in USD (unrecognized asset"),
        ([_challenge(asset="0x" + "d" * 40)], "only made in USDC on Base mainnet, contract 0x833589"),
        ([_challenge(amount="1000000")], "($1.0000) is above Unlisted's $0.05 cap"),
        ([_challenge(), _challenge(amount="1000000")], "spend controls refused to sign"),
        ([_challenge(), httpx.ConnectError("refused")], "couldn't reach the target again"),
        ([_challenge(), "ok"], "answered 200 instead of a 402 when Unlisted requested it again"),
    ],
)
def test_paid_402_without_test_payment_is_uncharged_reason_only(steps, reason):
    transport, seen = _scripted_target(*steps)
    resp, inbound = _paid_call(transport)

    assert resp.status_code == 422
    body = resp.json()
    assert list(body) == ["detail"]  # no check results: not a free Check
    assert reason in body["detail"]
    assert body["detail"].endswith(NOT_CHARGED)
    assert len(inbound.settle_calls) == 0
    assert main.paid_test_domain_limiter.current_count("target.seller.test") == 0
    assert not any("payment-signature" in r.headers for r in seen)


def test_paid_daily_ceiling_says_try_again_later(monkeypatch):
    monkeypatch.setattr(
        main, "economic_ceiling", EconomicCeiling(per_request_cap=0.05, global_ceiling=0.001, window_seconds=86400)
    )
    resp, inbound = _paid_call(_scripted_target(_challenge())[0])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "daily budget for test payments is used up" in detail
    assert "Try paid mode again later." in detail and detail.endswith(NOT_CHARGED)
    assert len(inbound.settle_calls) == 0
    assert main.paid_test_domain_limiter.current_count("target.seller.test") == 0


@pytest.mark.parametrize(
    "paid_step",
    [_challenge(), httpx.ConnectError("reset after send")],  # rejected, or failed in flight
)
def test_paid_attempted_payment_is_charged_whatever_the_outcome(paid_step):
    transport, seen = _scripted_target(_challenge(), _challenge(), paid_step)
    resp, inbound = _paid_call(transport)

    assert resp.status_code == 200
    settlement = [c for c in resp.json()["checks"] if c["check_id"] == "settlement_echo"][0]
    assert settlement["status"] == "fail"
    assert "payment-signature" in seen[-1].headers
    assert len(inbound.settle_calls) == 1
    assert main.paid_test_domain_limiter.current_count("target.seller.test") == 1


def test_paid_usdc_address_in_any_case_is_paid_and_charged():
    lower = _challenge(asset=outbound_payment.USDC_BASE_ADDRESS.lower())
    transport, seen = _scripted_target(lower, lower, lower)  # paid request rejected with a 402
    resp, inbound = _paid_call(transport)
    assert resp.status_code == 200
    assert "payment-signature" in seen[-1].headers
    assert len(inbound.settle_calls) == 1
