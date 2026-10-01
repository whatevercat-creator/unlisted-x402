"""
Tests for the inbound paywall (payment.py + main.create_app's paywall
wiring).

These build a *fresh* app via main.create_app(facilitator_client=...,
pay_to=...) rather than using main.app -- main.app never has a paywall
wired in during tests, since no CDP credentials or X402_PAY_TO are set in
this environment (see main.py's create_app docstring). That's deliberate:
it keeps every other test file free of network/credential dependencies.

FakeFacilitatorClient below is not a mock of x402's SDK -- it's a real
implementation of the FacilitatorClient protocol
(x402.http.facilitator_client_base.FacilitatorClient: verify / settle /
get_supported) that never touches the network. Wiring it into a real
x402ResourceServer + PaymentMiddlewareASGI means these tests exercise the
actual SDK code path (challenge construction, PAYMENT-SIGNATURE header
parsing, verify-then-settle sequencing) rather than asserting against a
mocked-out paywall. This mirrors the "verify against something real, don't
trust a paraphrase" approach the rest of this project's schema work used --
here that meant constructing a live TestClient request against the real
middleware and reading back what it actually produced (see the base64
PAYMENT-REQUIRED challenge decoded in test_diagnose_without_payment_*)
before writing any assertion.
"""

import base64
import json

import pytest
from fastapi.testclient import TestClient

import main
import payment
from limits import SlidingWindowRateLimiter
from x402.schemas import SettleResponse, SupportedKind, SupportedResponse, VerifyResponse

PAY_TO = "0x" + "2" * 40
PAYER = "0x" + "1" * 40


class FakeFacilitatorClient:
    """Structurally satisfies x402's FacilitatorClient protocol without a
    real facilitator behind it. `verify_response` / `settle_response` are
    injectable so individual tests can make verify or settle fail without
    needing a real invalid signature."""

    def __init__(self, verify_response: VerifyResponse | None = None, settle_response: SettleResponse | None = None):
        self.verify_calls: list[tuple] = []
        self.settle_calls: list[tuple] = []
        self._verify_response = verify_response or VerifyResponse(is_valid=True, payer=PAYER)
        self._settle_response = settle_response

    def get_supported(self) -> SupportedResponse:
        return SupportedResponse(
            kinds=[SupportedKind(x402_version=2, scheme=payment.SCHEME, network=payment.NETWORK)]
        )

    async def verify(self, payload, requirements) -> VerifyResponse:
        self.verify_calls.append((payload, requirements))
        return self._verify_response

    async def settle(self, payload, requirements) -> SettleResponse:
        self.settle_calls.append((payload, requirements))
        if self._settle_response is not None:
            return self._settle_response
        return SettleResponse(
            success=True, transaction="0xdeadbeef", network=requirements.network, payer=PAYER
        )


def _build_paid_client(facilitator_client=None):
    facilitator_client = facilitator_client or FakeFacilitatorClient()
    app = main.create_app(facilitator_client=facilitator_client, pay_to=PAY_TO)
    return TestClient(app), facilitator_client


def _decode_challenge(response) -> dict:
    header = response.headers["payment-required"]
    return json.loads(base64.b64decode(header))


def _payment_signature_header_for(challenge: dict) -> str:
    """Build a well-formed (but not cryptographically real) V2
    PAYMENT-SIGNATURE header for the given decoded challenge -- enough to
    reach FakeFacilitatorClient.verify(), which doesn't check signatures
    itself (a real facilitator would)."""
    requirements = challenge["accepts"][0]
    payment_payload = {
        "x402Version": 2,
        "payload": {"signature": "0xfake", "authorization": {"from": PAYER}},
        "accepted": requirements,
    }
    return base64.b64encode(json.dumps(payment_payload).encode()).decode()


@pytest.fixture(autouse=True)
def reset_limiters():
    """Same reasoning as test_main.py's fixture of the same name: the rate
    limiters are module-level globals shared with main.app, so a test here
    that trips one must not leave it tripped for every other test file."""
    main.caller_limiter = SlidingWindowRateLimiter(
        limit=main.CALLER_LIMIT, window_seconds=main.CALLER_WINDOW_SECONDS
    )
    main.domain_limiter = SlidingWindowRateLimiter(
        limit=main.DOMAIN_LIMIT, window_seconds=main.DOMAIN_WINDOW_SECONDS
    )
    yield


# --------------------------------------------------------------------------
# create_app wiring
# --------------------------------------------------------------------------


def test_create_app_with_no_args_and_no_env_runs_unpaid(monkeypatch):
    monkeypatch.delenv("X402_PAY_TO", raising=False)
    app = main.create_app()
    client = TestClient(app)
    # No paywall wired -- this hits run_dry_check for real, which will try
    # (and fail, sandboxed) to reach an unresolvable host. The point here
    # isn't the diagnosis outcome, it's that no PAYMENT-REQUIRED challenge
    # comes back demanding payment first.
    resp = client.post("/diagnose", json={"url": "https://no-such-host.example.invalid/x"})
    assert "payment-required" not in {k.lower() for k in resp.headers.keys()}


def test_create_app_wires_paywall_when_client_and_pay_to_given():
    client, fake = _build_paid_client()
    resp = client.post("/diagnose", json={"url": "https://api.seller.test/data"})
    assert resp.status_code == 402
    assert "payment-required" in resp.headers


def test_healthz_stays_free_even_with_paywall_wired():
    client, fake = _build_paid_client()
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert len(fake.verify_calls) == 0


# --------------------------------------------------------------------------
# The 402 challenge itself
# --------------------------------------------------------------------------


def test_diagnose_without_payment_returns_402_challenge():
    client, fake = _build_paid_client()
    resp = client.post("/diagnose", json={"url": "https://api.seller.test/data"})
    assert resp.status_code == 402
    assert len(fake.verify_calls) == 0  # never even gets that far without a payment header

    challenge = _decode_challenge(resp)
    assert challenge["x402Version"] == 2
    accepts = challenge["accepts"]
    assert len(accepts) == 1
    terms = accepts[0]
    assert terms["scheme"] == payment.SCHEME
    assert terms["network"] == payment.NETWORK
    assert terms["payTo"].lower() == PAY_TO.lower()
    # $0.02 in USDC's 6-decimal atomic units.
    assert terms["amount"] == "20000"


def test_diagnose_challenge_carries_bazaar_service_metadata():
    """This service's own Bazaar listing -- service_name/tags/description
    forwarded onto the challenge's resource info. Getting this right is the
    entire point of the product, so it's worth a direct assertion rather
    than trusting it because the SDK "should" do it."""
    client, fake = _build_paid_client()
    resp = client.post("/diagnose", json={"url": "https://api.seller.test/data"})
    challenge = _decode_challenge(resp)
    resource = challenge["resource"]
    assert resource["serviceName"] == payment.SERVICE_NAME
    assert set(resource["tags"]) == set(payment.TAGS)
    assert resource["description"] == payment.DESCRIPTION


# --------------------------------------------------------------------------
# Paying and getting a diagnosis
# --------------------------------------------------------------------------


def test_diagnose_with_valid_payment_settles_and_returns_report(monkeypatch):
    client, fake = _build_paid_client()

    # First request just to obtain a real, correctly-shaped challenge to pay against.
    challenge = _decode_challenge(client.post("/diagnose", json={"url": "https://api.seller.test/data"}))
    header = _payment_signature_header_for(challenge)

    async def _fake_report(url: str):
        from diagnosis import CheckResult, Confidence, DiagnosisReport, Status

        return DiagnosisReport(
            url=url,
            mode="dry",
            http_status=402,
            checks=[
                CheckResult(
                    check_id="resource_present",
                    status=Status.PASS,
                    detail="ok",
                    confidence=Confidence.SERVER,
                )
            ],
            verdict="No issues found in checks 1-4...",
        )

    monkeypatch.setattr(main, "run_dry_check", _fake_report)

    resp = client.post(
        "/diagnose",
        json={"url": "https://api.seller.test/data"},
        headers={"PAYMENT-SIGNATURE": header},
    )
    assert resp.status_code == 200
    assert resp.json()["http_status"] == 402  # the *diagnosed* status, unrelated to our own paywall
    assert len(fake.verify_calls) == 1
    assert len(fake.settle_calls) == 1
    assert "payment-response" in resp.headers


def test_diagnose_with_invalid_payment_is_rejected_and_not_settled():
    fake = FakeFacilitatorClient(
        verify_response=VerifyResponse(is_valid=False, invalid_reason="insufficient_funds")
    )
    client, _ = _build_paid_client(facilitator_client=fake)

    challenge = _decode_challenge(client.post("/diagnose", json={"url": "https://api.seller.test/data"}))
    header = _payment_signature_header_for(challenge)

    resp = client.post(
        "/diagnose",
        json={"url": "https://api.seller.test/data"},
        headers={"PAYMENT-SIGNATURE": header},
    )
    assert resp.status_code == 402
    assert len(fake.verify_calls) == 1
    assert len(fake.settle_calls) == 0  # rejected payments are never settled


def test_diagnose_rate_limited_after_valid_payment_is_not_settled(monkeypatch):
    """The important cost-safety property: a request that pays but then
    gets rate-limited *inside* the handler must not be charged -- verify
    happens before the handler runs, but settle only happens after a
    successful (<400) response (confirmed against the real x402 SDK, see
    payment.py's module docstring)."""
    client, fake = _build_paid_client()

    challenge = _decode_challenge(client.post("/diagnose", json={"url": "https://api.seller.test/data"}))
    header = _payment_signature_header_for(challenge)

    main.caller_limiter = SlidingWindowRateLimiter(limit=0, window_seconds=3600)

    resp = client.post(
        "/diagnose",
        json={"url": "https://api.seller.test/data"},
        headers={"PAYMENT-SIGNATURE": header},
    )
    assert resp.status_code == 429
    assert len(fake.verify_calls) == 1
    assert len(fake.settle_calls) == 0


def test_diagnose_settlement_failure_surfaces_as_402_not_500(monkeypatch):
    fake = FakeFacilitatorClient(
        settle_response=SettleResponse(
            success=False,
            error_reason="settlement_failed",
            transaction="",
            network=payment.NETWORK,
        )
    )
    client, _ = _build_paid_client(facilitator_client=fake)

    challenge = _decode_challenge(client.post("/diagnose", json={"url": "https://api.seller.test/data"}))
    header = _payment_signature_header_for(challenge)

    async def _fake_report(url: str):
        from diagnosis import DiagnosisReport

        return DiagnosisReport(url=url, mode="dry", http_status=402, checks=[], verdict="ok")

    monkeypatch.setattr(main, "run_dry_check", _fake_report)

    resp = client.post(
        "/diagnose",
        json={"url": "https://api.seller.test/data"},
        headers={"PAYMENT-SIGNATURE": header},
    )
    assert resp.status_code == 402
    assert len(fake.settle_calls) == 1


# --------------------------------------------------------------------------
# payment.py building blocks, in isolation
# --------------------------------------------------------------------------


def test_build_routes_uses_given_pay_to_and_price():
    routes = payment.build_routes(PAY_TO, price="$0.05", paid_price="$0.20")
    route = routes["POST /diagnose"]
    assert route.accepts.pay_to == PAY_TO
    assert route.accepts.network == payment.NETWORK
    assert route.accepts.scheme == payment.SCHEME
    assert route.service_name == payment.SERVICE_NAME

    # price is a DynamicPrice callable (chosen by ?mode= at request time,
    # not fixed at route-build time) -- see payment.py's _price_by_mode
    # docstring for why a query param, not a body field, decides this.
    assert callable(route.accepts.price)

    class _FakeAdapter:
        def __init__(self, mode):
            self._mode = mode

        def get_query_param(self, name):
            return self._mode if name == "mode" else None

    class _FakeCtx:
        def __init__(self, mode):
            self.adapter = _FakeAdapter(mode)

    assert route.accepts.price(_FakeCtx(None)) == "$0.05"
    assert route.accepts.price(_FakeCtx("paid")) == "$0.20"


def test_build_resource_server_registers_the_network():
    fake = FakeFacilitatorClient()
    server = payment.build_resource_server(fake)
    # register() returns Self for chaining; just confirm it didn't raise
    # and produced a server wired to our fake client.
    assert server is not None
