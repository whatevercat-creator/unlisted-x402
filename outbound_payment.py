"""
Outbound test-payment client for x402 Doctor -- build-order step 7.

This is the piece that actually spends real USDC: given a target's parsed
402 challenge, it builds a real, signed x402 payment (via a CDP-managed
wallet in production, or an injectable signer in tests) and replays the
original request with that payment attached, using the real x402 client
SDK end to end (x402Client + register_exact_evm_client + the SDK's own
x402AsyncTransport) -- verified empirically against the installed SDK
(x402==2.24.0) with a fake target seller (a second PaymentMiddlewareASGI
app talking to a fake facilitator) and a throwaway eth_account keypair
*before* being wired in here, the same "run the real thing against a
controlled double" approach the inbound paywall in payment.py was built
with.

Two safety layers sit between this module and an attacker-supplied URL,
both load-bearing and both independently confirmed:

1. Money. The caller (paid_check.py) only invokes this after checking the
   target's declared price against limits.EconomicCeiling -- but this
   module ALSO sets the x402 SDK's own client-side spend_controls
   (max_amount_per_payment) as a second, independent cap tied to the same
   number. Confirmed empirically: a price above spend_controls raises
   PaymentError before any request is even sent, so a bug in the caller's
   own price check doesn't translate into an unbounded real spend.
2. Network. Outbound requests never go through a bare httpx transport --
   they go through safe_fetch.SSRFSafeTransport, which re-resolves and
   IP-pins the target host on every single request this flow makes,
   including the payment-carrying retry. An attacker's URL doesn't get to
   skip SSRF protection just because this leg of the flow moves money
   instead of only reading a challenge.

Every entry point here is a plain function taking its dependencies
(signer, transport) as arguments rather than reaching for globals, so
tests exercise the exact same code paid_check.py calls in production,
just wired to fakes -- see tests/test_outbound_payment.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import httpx
from x402.client import x402Client
from x402.http.clients.httpx import PaymentError, x402AsyncTransport
from x402.http.utils import decode_payment_response_header
from x402.mechanisms.evm.exact import register_exact_evm_client

from safe_fetch import FetchError, SSRFBlocked, SSRFSafeTransport

# Only Base mainnet, matching payment.py's inbound paywall -- this service
# only knows how to test-pay what it can itself receive payment on.
NETWORK = "eip155:8453"
SCHEME = "exact"

# USDC's decimals on every network x402 currently uses it on -- matches the
# assumption the SDK's own money parser makes for "$X.XX"-style prices (see
# payment.py's DEFAULT_PRICE_USD comment for the inbound-side equivalent).
_USDC_DECIMALS = 6
_USDC_ASSET_NAME = "USD Coin"
# Circle's native USDC contract on Base mainnet (the x402 SDK's own default
# asset for eip155:8453). The only token a test payment is ever made in:
# `extra.name` is seller-supplied, so it can't identify the token alone.
USDC_BASE_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

DEFAULT_TEST_WALLET_NAME = "x402-doctor-test-wallet"


@dataclass
class PaymentTestOutcome:
    """What happened (or didn't) when attempting a real outbound test
    payment against a target's 402 challenge.

    `attempted=False` means no money could have moved -- either we
    declined to try (price unrecognized/too high, caught by the caller
    before this module runs) or the SDK's own spend_controls refused the
    payment before any request was sent. `attempted=True` means a
    payment-carrying request was actually sent; `success` then says
    whether the target's own settlement response confirms funds moved.
    """

    attempted: bool
    success: Optional[bool] = None
    skipped_reason: Optional[str] = None
    price_usd: Optional[float] = None
    final_status_code: Optional[int] = None
    settlement_transaction: Optional[str] = None
    settlement_error_reason: Optional[str] = None
    detail: str = ""


def usd_price_of_accept(accept: dict[str, Any]) -> Optional[float]:
    """Best-effort USD price of one `accepts[]` entry (as returned by
    diagnosis.extract_accepts), or None if we can't confidently price it.

    Deliberately conservative: an unrecognized asset means "we don't know
    the price," never "assume it's cheap." Callers should treat None as a
    reason to skip the real payment test, not a reason to guess.
    """
    if accept.get("network") != NETWORK:
        return None
    asset = accept.get("asset")
    if not isinstance(asset, str) or asset.lower() != USDC_BASE_ADDRESS.lower():
        return None
    extra = accept.get("extra") or {}
    if extra.get("name") != _USDC_ASSET_NAME:
        return None
    amount = accept.get("amount")
    if amount is None:
        return None
    try:
        return int(amount) / (10**_USDC_DECIMALS)
    except (TypeError, ValueError):
        return None


def select_payable_accept(accepts: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """The first accepts[] entry this service can actually pay against:
    our one supported scheme/network. Real challenges can list multiple
    scheme/network combinations; we only ever hold funds on one."""
    for accept in accepts:
        if accept.get("scheme") == SCHEME and accept.get("network") == NETWORK:
            return accept
    return None


def build_client(signer: Any, *, max_price_usd: float) -> x402Client:
    """A fresh x402Client per attempt (never reused/shared across
    requests), registered for our one scheme/network, with spend_controls
    capped at `max_price_usd` -- see this module's docstring for why this
    is a second, independent cap rather than trusting the caller's own
    price check alone. `signer` is anything register_exact_evm_client
    accepts: an eth_account LocalAccount (tests use a throwaway one), a
    CDP EvmLocalAccount (production), or anything structurally compatible
    with x402's ClientEvmSigner protocol.
    """
    client = x402Client()
    register_exact_evm_client(client, signer, networks=[NETWORK])
    client.set_spend_controls({"max_amount_per_payment": f"${max_price_usd:.6f}"})
    return client


async def attempt_payment(
    url: str,
    signer: Any,
    *,
    max_price_usd: float,
    price_usd: Optional[float] = None,
    transport: Optional[httpx.AsyncBaseTransport] = None,
    timeout: float = 20.0,
    method: str = "GET",
    json_body: Optional[Any] = None,
) -> PaymentTestOutcome:
    """Replay `url` (with `method`, and `json_body` for POST targets) with a
    real, signed x402 payment attached, and
    report what happened.

    `transport` defaults to a fresh SSRFSafeTransport (real network,
    IP-pinned per request, per this module's docstring); tests pass an
    httpx.ASGITransport pointed at an in-process fake target instead --
    the same substitution safe_fetch() supports for its own `transport`
    parameter, and the one this module's own development used to validate
    the whole flow offline before any of this touched the network.

    Raises SSRFBlocked / FetchError exactly as safe_fetch() does, for the
    same reason: "couldn't safely reach this host at all" is a different,
    non-diagnostic outcome from "reached it and something about payment
    didn't work," and callers (paid_check.py) should treat them
    differently rather than reporting a blocked SSRF probe as a seller bug.
    """
    client = build_client(signer, max_price_usd=max_price_usd)
    # Records whether a payment-carrying request was ever handed to the
    # network: the SDK wraps every error after its first, unpaid request in
    # PaymentError, so the exception type alone can't say whether a signed
    # payment went out.
    recorder = _PaymentSentRecorder(transport if transport is not None else SSRFSafeTransport())
    payment_transport = x402AsyncTransport(client, transport=recorder)

    try:
        async with httpx.AsyncClient(transport=payment_transport, timeout=timeout) as http:
            response = await http.request(method, url, json=json_body)
    except PaymentError as e:
        if recorder.sent:
            return PaymentTestOutcome(
                attempted=True,
                success=False,
                price_usd=price_usd,
                detail=f"The payment-carrying request failed: {e}",
            )
        # Refused before anything was sent -- normally the SDK's own
        # spend_controls, which should only trip if the target's price
        # changed between our dry-check pricing decision and this attempt
        # (a race, not a caller bug), since paid_check.py already checks
        # the price against the same cap before ever calling this function.
        return PaymentTestOutcome(
            attempted=False,
            skipped_reason=f"payment_client_rejected: {e}",
            price_usd=price_usd,
            detail=(
                "The x402 payment client's spend controls refused to sign the payment "
                "(the target's price may have changed since it was first read), so no "
                "test payment was made."
            ),
        )
    except (SSRFBlocked, FetchError) as e:
        if recorder.sent:
            return PaymentTestOutcome(
                attempted=True,
                success=False,
                price_usd=price_usd,
                detail=f"The payment-carrying request failed: {e}",
            )
        raise
    except httpx.HTTPError as e:
        if not recorder.sent:
            return PaymentTestOutcome(
                attempted=False,
                skipped_reason=f"unreachable: {e}",
                price_usd=price_usd,
                detail="Unlisted couldn't reach the target again to make the test payment, so none was made.",
            )
        kind = "Timed out waiting for a response" if isinstance(e, httpx.TimeoutException) else "Transport error"
        return PaymentTestOutcome(
            attempted=True,
            success=False,
            price_usd=price_usd,
            detail=f"{kind} after submitting payment: {e}",
        )

    if not recorder.sent:
        return PaymentTestOutcome(
            attempted=False,
            skipped_reason=f"no_402_on_retry: {response.status_code}",
            price_usd=price_usd,
            final_status_code=response.status_code,
            detail=(
                f"The target answered {response.status_code} instead of a 402 when Unlisted "
                "requested it again to pay, so no test payment was made."
            ),
        )

    return _interpret_response(response, price_usd=price_usd)


_PAYMENT_HEADERS = ("payment-signature", "x-payment")


class _PaymentSentRecorder(httpx.AsyncBaseTransport):
    """Pass-through transport that notes when a request carrying an x402
    payment header is handed to the inner transport. Set before the send,
    so a payment counts as attempted even if that request then fails."""

    def __init__(self, inner: httpx.AsyncBaseTransport):
        self._inner = inner
        self.sent = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if any(h in request.headers for h in _PAYMENT_HEADERS):
            self.sent = True
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def _interpret_response(response: httpx.Response, *, price_usd: Optional[float]) -> PaymentTestOutcome:
    settlement_header = response.headers.get("payment-response") or response.headers.get(
        "x-payment-response"
    )

    if response.status_code == 200 and settlement_header:
        try:
            settle = decode_payment_response_header(settlement_header)
        except Exception as e:  # malformed settlement header is still a real signal
            return PaymentTestOutcome(
                attempted=True,
                success=False,
                price_usd=price_usd,
                final_status_code=response.status_code,
                detail=f"Paid successfully but couldn't parse the settlement response header: {e}",
            )
        return PaymentTestOutcome(
            attempted=True,
            success=bool(settle.success),
            price_usd=price_usd,
            final_status_code=response.status_code,
            settlement_transaction=settle.transaction or None,
            settlement_error_reason=settle.error_reason,
            detail=(
                "Payment settled successfully; the endpoint returned the paid resource."
                if settle.success
                else "Endpoint returned 200 but its own settlement response reports "
                f"failure: {settle.error_reason or 'no reason given'}."
            ),
        )

    if response.status_code == 200 and not settlement_header:
        return PaymentTestOutcome(
            attempted=True,
            success=False,
            price_usd=price_usd,
            final_status_code=response.status_code,
            detail=(
                "Endpoint returned 200 to our payment-carrying request but never sent "
                "a PAYMENT-RESPONSE settlement header -- either it doesn't consistently "
                "require payment for this resource, or it settles without echoing "
                "confirmation back to the buyer."
            ),
        )

    if response.status_code == 402:
        return PaymentTestOutcome(
            attempted=True,
            success=False,
            price_usd=price_usd,
            final_status_code=response.status_code,
            detail=(
                "We submitted a validly-signed payment for the exact amount requested, "
                "and the endpoint still responded 402 -- its facilitator or settlement "
                "logic is rejecting a well-formed payment."
            ),
        )

    return PaymentTestOutcome(
        attempted=True,
        success=False,
        price_usd=price_usd,
        final_status_code=response.status_code,
        detail=(
            f"After submitting payment, the endpoint returned an unexpected status "
            f"{response.status_code} instead of the paid resource."
        ),
    )


async def build_live_signer(wallet_name: str = DEFAULT_TEST_WALLET_NAME):
    """The real CDP-managed signer for production use.

    Imports the CDP SDK lazily, for the same reason payment.py's
    build_live_facilitator_client does: keeps this module importable
    without the CDP SDK's own import-time side effects (its Bazaar-ToS
    notice print) unless this path is actually used.

    Reads CDP_API_KEY_ID / CDP_API_KEY_SECRET / CDP_WALLET_SECRET from the
    environment via CdpClient's own defaults. `wallet_name` should name a
    wallet dedicated to *outbound* test payments -- separate from both
    crypto-sentiment-x402's revenue wallet and x402 Doctor's own
    X402_PAY_TO receiving wallet (payment.py), since this is the one
    wallet in the whole system that spends rather than receives, and
    needs a small funded balance rather than a sweep destination.

    cdp.evm_local_account.EvmLocalAccount is the documented bridge here:
    it wraps an async CDP server account (from cdp.evm.get_or_create_account)
    in a synchronous, eth_account-compatible signer -- confirmed by reading
    its source, which explicitly exists so CDP-managed keys can be used
    anywhere an eth_account LocalAccount is expected (exactly the
    `register_exact_evm_client` call in build_client above).
    """
    from cdp import CdpClient
    from cdp.evm_local_account import EvmLocalAccount

    cdp = CdpClient()
    server_account = await cdp.evm.get_or_create_account(name=wallet_name)
    return EvmLocalAccount(server_account)
