"""
Tests for paid_check.py -- the orchestration between the free dry checks
and a real outbound test payment.

Uses the same fake-target-app-via-ASGITransport approach as
test_outbound_payment.py for the "actually attempt payment" path, plus a
FakeClock-driven EconomicCeiling (see test_limits.py) so the price-cap and
daily-ceiling gates can be tested deterministically without real time
passing.
"""

import httpx
import pytest
from eth_account import Account
from fastapi import FastAPI

from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http.types import PaymentOption, RouteConfig
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.schemas import SupportedKind, SupportedResponse, VerifyResponse, SettleResponse
from x402.server import x402ResourceServer

import outbound_payment
import safe_fetch as sf
from diagnosis import Status
from limits import EconomicCeiling
from paid_check import run_paid_check
from safe_fetch import FetchError, ResolvedTarget, SSRFBlocked
from tests.test_bazaar import FakeBazaarClient, make_response

SIGNER = Account.create()


@pytest.fixture(autouse=True)
def fake_resolver(monkeypatch):
    """run_paid_check's initial dry-fetch leg goes through safe_fetch(),
    which always calls resolve_and_validate() for real DNS -- irrelevant
    here since the ASGITransport used throughout this file never actually
    connects anywhere, but safe_fetch() doesn't know that. Same pattern as
    test_safe_fetch.py's fixture of the same name."""

    async def _fake(hostname: str, port: int) -> ResolvedTarget:
        return ResolvedTarget(hostname=hostname, port=port, ip="1.2.3.4", family=2)

    monkeypatch.setattr(sf, "resolve_and_validate", _fake)


class FakeSellerFacilitator:
    """WARNING, confirmed the hard way while writing these tests: if
    get_supported() ever reports a different scheme/network than what the
    route + x402ResourceServer.register() actually use, x402's own
    x402HTTPResourceServer.initialize() treats that as a *fatal*
    configuration error and calls os._exit(1) -- an unconditional, hard
    process kill, not a raisable Python exception. Under pytest that
    silently kills the whole test run with no traceback; in production it
    would kill the entire server process on the first request after a
    facilitator/route mismatch. Keep `network` here matched to whatever
    the test registers the server for -- see
    test_paid_check_skips_when_no_payable_accept below for a test that
    deliberately uses a *different* network and passes its own matching
    facilitator instance instead of this one.
    """

    def __init__(self, verify_ok=True, network=None):
        self.settle_calls = []
        self._verify_ok = verify_ok
        self._network = network or outbound_payment.NETWORK

    def get_supported(self):
        return SupportedResponse(
            kinds=[SupportedKind(x402_version=2, scheme=outbound_payment.SCHEME, network=self._network)]
        )

    async def verify(self, payload, requirements):
        if self._verify_ok:
            return VerifyResponse(is_valid=True, payer=SIGNER.address)
        return VerifyResponse(is_valid=False, invalid_reason="insufficient_funds")

    async def settle(self, payload, requirements):
        self.settle_calls.append((payload, requirements))
        return SettleResponse(
            success=True, transaction="0xdeadbeef", network=requirements.network, payer=SIGNER.address
        )


def make_target_app(price="$0.01", asset_name="USD Coin", verify_ok=True):
    app = FastAPI()

    @app.get("/sentiment/BTC")
    async def resource():
        return {"sentiment": "bullish"}

    fake = FakeSellerFacilitator(verify_ok=verify_ok)
    server = x402ResourceServer(fake)
    server.register(outbound_payment.NETWORK, ExactEvmServerScheme())
    extra = {"name": asset_name, "version": "2"} if asset_name else None
    accepts = PaymentOption(
        scheme=outbound_payment.SCHEME,
        pay_to="0x" + "9" * 40,
        price=price,
        network=outbound_payment.NETWORK,
        extra=extra,
    )
    routes = {"GET /sentiment/BTC": RouteConfig(accepts=accepts)}
    app.add_middleware(PaymentMiddlewareASGI, routes=routes, server=server)
    return app, fake


class FakeClock:
    def __init__(self, start: float = 1_000_000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t


def make_ceiling(per_request_cap=0.05, global_ceiling=2.00, clock=None):
    return EconomicCeiling(
        per_request_cap=per_request_cap,
        global_ceiling=global_ceiling,
        window_seconds=86400,
        clock=clock or FakeClock(),
    )


URL = "http://target.test/sentiment/BTC"


def settlement_check(report):
    matches = [c for c in report.checks if c.check_id == "settlement_echo"]
    assert len(matches) == 1
    return matches[0]


async def test_paid_check_succeeds_and_records_spend():
    app, fake = make_target_app(price="$0.01")
    ceiling = make_ceiling()

    report = await run_paid_check(
        URL, signer=SIGNER, economic_ceiling=ceiling, transport=httpx.ASGITransport(app=app)
    )

    assert report.mode == "paid"
    assert report.http_status == 402  # the *dry* fetch's status -- unrelated to our own payment
    check = settlement_check(report)
    assert check.status == Status.PASS
    assert len(fake.settle_calls) == 1
    assert ceiling.current_spend() == pytest.approx(0.01)


async def test_paid_check_skips_when_price_exceeds_cap_and_records_no_spend():
    app, fake = make_target_app(price="$1.00")
    ceiling = make_ceiling(per_request_cap=0.05)

    report = await run_paid_check(
        URL, signer=SIGNER, economic_ceiling=ceiling, transport=httpx.ASGITransport(app=app)
    )

    check = settlement_check(report)
    assert check.status == Status.SKIP
    assert "cap" in check.detail.lower()
    assert len(fake.settle_calls) == 0
    assert ceiling.current_spend() == 0.0


async def test_paid_check_skips_when_daily_ceiling_exhausted():
    app, fake = make_target_app(price="$0.01")
    clock = FakeClock()
    ceiling = make_ceiling(per_request_cap=0.05, global_ceiling=1.00, clock=clock)
    ceiling.record_spend(1.00)  # already at the ceiling

    report = await run_paid_check(
        URL, signer=SIGNER, economic_ceiling=ceiling, transport=httpx.ASGITransport(app=app)
    )

    check = settlement_check(report)
    assert check.status == Status.SKIP
    assert "budget" in check.detail.lower() or "ceiling" in check.detail.lower()
    assert len(fake.settle_calls) == 0


async def test_paid_check_skips_unrecognized_asset_without_guessing_price():
    app, fake = make_target_app(price="$0.01", asset_name="Some Wrapped Token")
    ceiling = make_ceiling()

    report = await run_paid_check(
        URL, signer=SIGNER, economic_ceiling=ceiling, transport=httpx.ASGITransport(app=app)
    )

    check = settlement_check(report)
    assert check.status == Status.SKIP
    assert "unrecognized" in check.detail.lower() or "USD" in check.detail
    assert len(fake.settle_calls) == 0


async def test_paid_check_skips_when_no_payable_accept():
    """Target only offers a scheme/network we don't hold funds on."""
    app = FastAPI()

    @app.get("/sentiment/BTC")
    async def resource():
        return {}

    fake = FakeSellerFacilitator(network="eip155:1")
    server = x402ResourceServer(fake)
    server.register("eip155:1", ExactEvmServerScheme())  # ethereum mainnet, not ours
    routes = {
        "GET /sentiment/BTC": RouteConfig(
            accepts=PaymentOption(
                scheme=outbound_payment.SCHEME, pay_to="0x" + "9" * 40, price="$0.01", network="eip155:1"
            )
        )
    }
    app.add_middleware(PaymentMiddlewareASGI, routes=routes, server=server)

    ceiling = make_ceiling()
    report = await run_paid_check(
        URL, signer=SIGNER, economic_ceiling=ceiling, transport=httpx.ASGITransport(app=app)
    )
    check = settlement_check(report)
    assert check.status == Status.SKIP
    assert "scheme" in check.detail.lower() or "network" in check.detail.lower()


async def test_paid_check_reports_failure_when_target_rejects_valid_payment():
    app, fake = make_target_app(price="$0.01", verify_ok=False)
    ceiling = make_ceiling()

    report = await run_paid_check(
        URL, signer=SIGNER, economic_ceiling=ceiling, transport=httpx.ASGITransport(app=app)
    )

    check = settlement_check(report)
    assert check.status == Status.FAIL
    assert ceiling.current_spend() == 0.0  # rejected payment -- nothing spent


async def test_paid_check_runs_the_ordinary_dry_checks_too():
    """Paid mode doesn't replace checks 1-5 -- it adds to them."""
    app, fake = make_target_app(price="$0.01")
    ceiling = make_ceiling()

    report = await run_paid_check(
        URL, signer=SIGNER, economic_ceiling=ceiling, transport=httpx.ASGITransport(app=app)
    )

    check_ids = {c.check_id for c in report.checks}
    assert "settlement_echo" in check_ids
    assert len(check_ids) > 1  # the ordinary dry-check rule chain also ran


async def test_paid_check_omits_bazaar_check_when_no_client_given():
    app, fake = make_target_app(price="$0.01")
    ceiling = make_ceiling()

    report = await run_paid_check(
        URL, signer=SIGNER, economic_ceiling=ceiling, transport=httpx.ASGITransport(app=app)
    )

    assert "bazaar_index_status" not in {c.check_id for c in report.checks}


async def test_paid_check_includes_bazaar_check_when_client_given():
    app, fake = make_target_app(price="$0.01")
    ceiling = make_ceiling()
    bazaar_client = FakeBazaarClient(response=make_response(active=True))

    # https, not the module-level `URL` constant (http://target.test/...) --
    # CDP's real validate endpoint only accepts https:// resource URLs (see
    # bazaar.py), and bazaar.check_bazaar_index_status now checks that
    # itself before ever touching the SDK, so an http:// URL here would
    # legitimately SKIP rather than reach the fake client at all. ASGITransport
    # routes on path, not scheme/host, so this still hits the same fake app.
    https_url = "https://target.test/sentiment/BTC"

    report = await run_paid_check(
        https_url,
        signer=SIGNER,
        economic_ceiling=ceiling,
        transport=httpx.ASGITransport(app=app),
        bazaar_client=bazaar_client,
    )

    statuses = {c.check_id: c.status for c in report.checks}
    assert statuses["bazaar_index_status"] == Status.PASS
    assert len(bazaar_client.calls) == 1


async def test_paid_check_bazaar_check_skips_cleanly_for_non_https_target():
    app, fake = make_target_app(price="$0.01")
    ceiling = make_ceiling()
    bazaar_client = FakeBazaarClient(response=make_response(active=True))

    report = await run_paid_check(
        URL,  # http://target.test/... -- CDP's validate API can't check this
        signer=SIGNER,
        economic_ceiling=ceiling,
        transport=httpx.ASGITransport(app=app),
        bazaar_client=bazaar_client,
    )

    statuses = {c.check_id: c.status for c in report.checks}
    assert statuses["bazaar_index_status"] == Status.SKIP
    assert len(bazaar_client.calls) == 0  # never even reached the client
