"""
Tests for outbound_payment.py -- the module that actually spends money.

The fake target below is a *real* PaymentMiddlewareASGI-protected FastAPI
app (the same pattern test_payment.py uses for the inbound side), reached
via httpx.ASGITransport instead of a real network connection. The signer is
a throwaway eth_account keypair (Account.create()) -- a real cryptographic
identity, never funded, never used anywhere else -- so the "exact" EVM
scheme's client-side signing runs for real (real EIP-712 signatures) rather
than being mocked out. Only the facilitator on each side is fake (so
verify/settle never touch the network or a real chain).

This mirrors exactly how this integration was first validated by hand
before any of it was wired into paid_check.py or main.py -- see
outbound_payment.py's module docstring.
"""

import httpx
import pytest
from eth_account import Account
from fastapi import FastAPI

from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http.types import PaymentOption, RouteConfig
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.schemas import SettleResponse, SupportedKind, SupportedResponse, VerifyResponse
from x402.server import x402ResourceServer

import outbound_payment
from outbound_payment import (
    PaymentTestOutcome,
    attempt_payment,
    select_payable_accept,
    usd_price_of_accept,
)
from safe_fetch import FetchError, SSRFBlocked

SELLER_PAY_TO = "0x" + "9" * 40
SIGNER = Account.create()  # throwaway, test-only keypair


class FakeSellerFacilitator:
    def __init__(self, verify_ok=True, settle_response=None):
        self.verify_calls = []
        self.settle_calls = []
        self._verify_ok = verify_ok
        self._settle_response = settle_response

    def get_supported(self):
        return SupportedResponse(
            kinds=[
                SupportedKind(
                    x402_version=2, scheme=outbound_payment.SCHEME, network=outbound_payment.NETWORK
                )
            ]
        )

    async def verify(self, payload, requirements):
        self.verify_calls.append((payload, requirements))
        if self._verify_ok:
            return VerifyResponse(is_valid=True, payer=SIGNER.address)
        return VerifyResponse(is_valid=False, invalid_reason="insufficient_funds")

    async def settle(self, payload, requirements):
        self.settle_calls.append((payload, requirements))
        if self._settle_response is not None:
            return self._settle_response
        return SettleResponse(
            success=True, transaction="0xdeadbeef", network=requirements.network, payer=SIGNER.address
        )


def make_target_app(price="$0.01", verify_ok=True, settle_response=None, path="/resource"):
    app = FastAPI()

    @app.get(path)
    async def resource():
        return {"sentiment": "bullish"}

    fake = FakeSellerFacilitator(verify_ok=verify_ok, settle_response=settle_response)
    server = x402ResourceServer(fake)
    server.register(outbound_payment.NETWORK, ExactEvmServerScheme())
    routes = {
        f"GET {path}": RouteConfig(
            accepts=PaymentOption(
                scheme=outbound_payment.SCHEME,
                pay_to=SELLER_PAY_TO,
                price=price,
                network=outbound_payment.NETWORK,
            ),
        )
    }
    app.add_middleware(PaymentMiddlewareASGI, routes=routes, server=server)
    return app, fake


def transport_for(app) -> httpx.ASGITransport:
    return httpx.ASGITransport(app=app)


# --------------------------------------------------------------------------
# usd_price_of_accept / select_payable_accept
# --------------------------------------------------------------------------


def _usdc_accept(**overrides):
    accept = {
        "network": "eip155:8453",
        "asset": outbound_payment.USDC_BASE_ADDRESS,
        "amount": "20000",
        "extra": {"name": "USD Coin", "version": "2"},
    }
    accept.update(overrides)
    return accept


def test_usdc_base_address_matches_the_sdk_default_asset():
    from x402.mechanisms.evm.default_assets import DEFAULT_ASSETS

    assert outbound_payment.USDC_BASE_ADDRESS in [a["asset"] for a in DEFAULT_ASSETS["eip155:8453"]]


def test_usd_price_of_accept_recognizes_usdc():
    assert usd_price_of_accept(_usdc_accept()) == pytest.approx(0.02)


def test_usd_price_of_accept_ignores_address_case():
    for asset in (outbound_payment.USDC_BASE_ADDRESS.lower(), outbound_payment.USDC_BASE_ADDRESS.upper().replace("0X", "0x")):
        assert usd_price_of_accept(_usdc_accept(asset=asset)) == pytest.approx(0.02)


def test_usd_price_of_accept_rejects_other_token_named_usd_coin():
    assert usd_price_of_accept(_usdc_accept(asset="0x" + "d" * 40)) is None
    assert usd_price_of_accept(_usdc_accept(asset=None)) is None


def test_usd_price_of_accept_rejects_other_network():
    assert usd_price_of_accept(_usdc_accept(network="eip155:1")) is None


def test_usd_price_of_accept_returns_none_for_unrecognized_asset():
    assert usd_price_of_accept(_usdc_accept(extra={"name": "Some Other Token"})) is None


def test_usd_price_of_accept_returns_none_when_amount_missing():
    accept = _usdc_accept()
    del accept["amount"]
    assert usd_price_of_accept(accept) is None


def test_select_payable_accept_picks_our_scheme_and_network():
    accepts = [
        {"scheme": "exact", "network": "eip155:84532"},  # testnet, not ours
        {"scheme": "exact", "network": "eip155:8453"},  # ours
    ]
    picked = select_payable_accept(accepts)
    assert picked == accepts[1]


def test_select_payable_accept_returns_none_when_nothing_matches():
    assert select_payable_accept([{"scheme": "exact", "network": "eip155:1"}]) is None


# --------------------------------------------------------------------------
# attempt_payment against a real (fake-facilitator) target, end to end
# --------------------------------------------------------------------------


async def test_attempt_payment_succeeds_against_a_healthy_target():
    app, fake = make_target_app(price="$0.01")
    outcome = await attempt_payment(
        "http://target.test/resource",
        SIGNER,
        max_price_usd=0.05,
        price_usd=0.01,
        transport=transport_for(app),
    )
    assert outcome.attempted is True
    assert outcome.success is True
    assert outcome.final_status_code == 200
    assert outcome.settlement_transaction == "0xdeadbeef"
    assert len(fake.verify_calls) == 1
    assert len(fake.settle_calls) == 1


async def test_attempt_payment_reports_target_rejection_as_failure_not_exception():
    app, fake = make_target_app(price="$0.01", verify_ok=False)
    outcome = await attempt_payment(
        "http://target.test/resource",
        SIGNER,
        max_price_usd=0.05,
        price_usd=0.01,
        transport=transport_for(app),
    )
    assert outcome.attempted is True
    assert outcome.success is False
    assert outcome.final_status_code == 402
    assert len(fake.settle_calls) == 0  # never reached settlement


async def test_attempt_payment_reports_settlement_failure_despite_200():
    """A seller whose middleware doesn't follow the "don't return the
    resource unless settlement succeeded" contract -- a different
    implementation than the reference x402 SDK might get this wrong, and
    this is exactly the kind of bug this tool exists to catch."""
    app, fake = make_target_app(
        price="$0.01",
        settle_response=SettleResponse(
            success=False, error_reason="chain_error", transaction="", network=outbound_payment.NETWORK
        ),
    )
    outcome = await attempt_payment(
        "http://target.test/resource",
        SIGNER,
        max_price_usd=0.05,
        price_usd=0.01,
        transport=transport_for(app),
    )
    # The real SDK's own middleware won't return 200 on settle failure (see
    # payment.py's docstring), so this specific seller returns the 402 it
    # produces instead -- still correctly reported as an unsuccessful test.
    assert outcome.success is False


async def test_attempt_payment_is_refused_by_spend_controls_over_cap():
    app, _ = make_target_app(price="$1.00")
    outcome = await attempt_payment(
        "http://target.test/resource",
        SIGNER,
        max_price_usd=0.05,
        price_usd=1.00,
        transport=transport_for(app),
    )
    assert outcome.attempted is False
    assert "spend control" in outcome.detail.lower() or "spend_control" in (outcome.skipped_reason or "")


async def test_attempt_payment_propagates_ssrf_blocked_from_transport():
    """Not a target-behavior outcome -- if the transport itself refuses the
    connection (a real SSRFSafeTransport would, for a blocked IP), that's a
    "couldn't safely check this" case, not a "target failed the test" case,
    and must surface as an exception, not a PaymentTestOutcome."""

    class BlockingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise SSRFBlocked("blocked for this test")

    with pytest.raises(SSRFBlocked):
        await attempt_payment(
            "http://target.test/resource",
            SIGNER,
            max_price_usd=0.05,
            price_usd=0.01,
            transport=BlockingTransport(),
        )


class _ScriptedTransport(httpx.AsyncBaseTransport):
    """Sends unpaid requests to `app`; answers a payment-carrying one with
    `on_paid` (an exception to raise) or passes it through."""

    def __init__(self, app, on_paid=None, unpaid_status=None):
        self._inner = httpx.ASGITransport(app=app)
        self._on_paid = on_paid
        self._unpaid_status = unpaid_status

    async def handle_async_request(self, request):
        if "payment-signature" in request.headers and self._on_paid is not None:
            raise self._on_paid
        if self._unpaid_status is not None:
            return httpx.Response(self._unpaid_status)
        return await self._inner.handle_async_request(request)


@pytest.mark.parametrize("error", [httpx.ConnectError("reset"), FetchError("reset"), httpx.ReadTimeout("slow")])
async def test_attempt_payment_counts_a_failed_payment_request_as_attempted(error):
    app, _ = make_target_app(price="$0.01")
    outcome = await attempt_payment(
        "http://target.test/resource", SIGNER, max_price_usd=0.05, price_usd=0.01,
        transport=_ScriptedTransport(app, on_paid=error),
    )
    assert outcome.attempted is True
    assert outcome.success is False


async def test_attempt_payment_not_attempted_when_retry_isnt_a_402():
    app, _ = make_target_app(price="$0.01")
    outcome = await attempt_payment(
        "http://target.test/resource", SIGNER, max_price_usd=0.05, price_usd=0.01,
        transport=_ScriptedTransport(app, unpaid_status=200),
    )
    assert outcome.attempted is False
    assert outcome.skipped_reason == "no_402_on_retry: 200"


async def test_attempt_payment_not_attempted_when_unreachable_before_paying():
    class Failing(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectTimeout("no route")

    outcome = await attempt_payment(
        "http://target.test/resource", SIGNER, max_price_usd=0.05, price_usd=0.01, transport=Failing()
    )
    assert outcome.attempted is False
    assert outcome.skipped_reason.startswith("unreachable")


def test_select_payable_accept_skips_permit2_options():
    eip3009 = {"scheme": "exact", "network": "eip155:8453", "extra": {"name": "USD Coin"}}
    explicit = {"scheme": "exact", "network": "eip155:8453", "extra": {"assetTransferMethod": "eip3009"}}
    permit2 = {"scheme": "exact", "network": "eip155:8453", "extra": {"assetTransferMethod": "permit2"}}
    assert outbound_payment.select_payable_accept([permit2, eip3009]) is eip3009
    assert outbound_payment.select_payable_accept([explicit]) is explicit
    assert outbound_payment.select_payable_accept([permit2]) is None
    assert outbound_payment.on_our_rails(permit2)


def test_client_policy_only_keeps_usdc_eip3009_on_base():
    from x402.schemas import PaymentRequirements

    def req(**overrides):
        fields = dict(
            scheme="exact", network="eip155:8453", asset=outbound_payment.USDC_BASE_ADDRESS.lower(),
            amount="10000", pay_to=SELLER_PAY_TO, max_timeout_seconds=60, extra={"name": "USD Coin"},
        )
        fields.update(overrides)
        return PaymentRequirements(**fields)

    good = req()
    kept = outbound_payment._only_usdc_authorization(
        2, [good, req(extra={"assetTransferMethod": "permit2"}), req(asset="0x" + "d" * 40), req(network="eip155:1")]
    )
    assert kept == [good]
    assert outbound_payment._only_usdc_authorization(1, [good]) == []
