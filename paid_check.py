"""
Paid-check mode for x402 Doctor -- build-order step 7, the settlement-echo
variant of check 3 the scoping doc marked as blocked on paid mode existing.

Dry-check (dry_check.py) can only tell you the challenge *looks* correct.
This runs the same checks and then, when it's safe to, actually pays the
target a real amount via outbound_payment.py and observes whether payment
genuinely settles end to end -- the single biggest differentiator between
x402 Doctor and a purely schema-level checker like the existing prior-art
tool noted in the scoping doc (Fizzl13/x402-doctor, no real payment
simulation).

"Safe to" is deliberately conservative and check in this order, each with
its own SKIP reason surfaced in the report rather than silently declining:
1. the challenge must offer our one supported scheme/network (exact,
   Base mainnet) -- we can't pay what we can't sign for.
2. the price must be confidently resolvable to USD (a recognized USDC
   asset) -- an unrecognized asset means "unknown price," not "assume
   it's cheap."
3. the price must be within EconomicCeiling.price_within_cap -- the same
   per-request safety cap outbound_payment.py's spend_controls also
   enforces independently.
4. spending it must not breach EconomicCeiling.can_spend's rolling 24h
   ceiling.

Only after all four does this actually call outbound_payment.attempt_payment,
and only a *successful* attempt (funds genuinely confirmed moved, per the
target's own settlement response) calls economic_ceiling.record_spend --
mirroring exactly the verify-before-settle-after-success contract payment.py
documents for the *inbound* side: a real charge is recorded only once the
real-world effect it represents actually happened.

The per-target "don't real-payment-test the same domain more than once a
day" rule lives in main.py, not here, because it's a request-admission
decision (should this request even reach paid_check at all) rather than a
diagnosis-quality decision -- and because it must reject *before* any
payment is verified from the caller, so a blocked repeat-test is never
charged either.

Build-order step 9 adds the same optional Bazaar index-status check that
dry_check.py gained -- appended alongside settlement_echo, same
None-means-"not configured" contract, same reasoning (see dry_check.py's
module docstring for why this keeps every existing caller/test
unaffected).
"""

from __future__ import annotations

from typing import Any, Optional

from bazaar import check_bazaar_index_status, lookup_curated
from diagnosis import (
    ChallengeParseError,
    CheckResult,
    Confidence,
    DiagnosisReport,
    Status,
    decode_challenge,
    extract_accepts,
    non_402_verdict,
    run_checks,
    summarize,
)
from limits import EconomicCeiling
from outbound_payment import (
    PaymentTestOutcome,
    attempt_payment,
    on_our_rails,
    select_payable_accept,
    USDC_BASE_ADDRESS,
    usd_price_of_accept,
)
from dry_check import _body_kwargs
from probe import check_probe_response
from safe_fetch import FetchError, SSRFBlocked, safe_fetch


async def run_paid_check(
    url: str,
    *,
    signer: Any,
    economic_ceiling: EconomicCeiling,
    transport: Optional[Any] = None,
    bazaar_client: Optional[Any] = None,
    method: str = "GET",
    json_body: Optional[Any] = None,
) -> DiagnosisReport:
    """Run the dry checks against `url`, then attempt a real outbound test
    payment if (and only if) it's safe to, per this module's docstring.

    Same raising contract as dry_check.run_dry_check: SSRFBlocked and
    FetchError from the *initial* fetch are the only ways this still
    raises -- a problem specific to the payment-attempt leg is instead
    folded into the report as a `settlement_echo` CheckResult, since by
    that point there's already a valid report worth returning.

    `signer` and `transport` are passed straight through to both the
    initial dry fetch (safe_fetch's own `transport` parameter) and
    outbound_payment.attempt_payment -- in production both default to real
    network use (safe_fetch's own IP-pinning, and a fresh SSRFSafeTransport,
    respectively); tests point both at the same in-process fake target via
    one shared httpx.ASGITransport, exactly like an ordinary browser-style
    request against a real seller would exercise the same challenge and
    the same paid retry.
    """
    response = await safe_fetch(url, method=method, transport=transport, **_body_kwargs(json_body))

    if response.status_code != 402:
        return DiagnosisReport(
            url=url,
            mode="paid",
            http_status=response.status_code,
            checks=[],
            verdict=non_402_verdict(
                response.status_code, method=method, mode="paid", allow=response.headers.get("allow")
            ),
        )

    try:
        challenge = decode_challenge(response.headers.get("payment-required"), response.body)
    except ChallengeParseError as e:
        return DiagnosisReport(
            url=url,
            mode="paid",
            http_status=response.status_code,
            checks=[],
            verdict="Could not parse the 402 response as an x402 challenge.",
            parse_error=str(e),
            unpaid_reason=(
                "The target returned a 402, but it couldn't be parsed as an x402 "
                "challenge, so no test payment could be made."
            ),
        )

    checks = run_checks(challenge, final_url=response.url)

    async def _probe_fetch(probe_url: str, **kwargs: Any):
        return await safe_fetch(probe_url, transport=transport, **kwargs)

    checks.append(await check_probe_response(challenge, response.url, _probe_fetch, method))
    accepts = extract_accepts(challenge)

    settlement_check, unpaid_reason = await _run_settlement_echo_check(
        url=response.url,
        accepts=accepts,
        signer=signer,
        economic_ceiling=economic_ceiling,
        transport=transport,
        method=method,
        json_body=json_body,
    )
    checks.append(settlement_check)

    bazaar_check = await check_bazaar_index_status(response.url, bazaar_client, method)
    if bazaar_check is not None:
        checks.append(bazaar_check)
    curated = await lookup_curated(challenge, response.url, bazaar_client, bazaar_check)

    return DiagnosisReport(
        url=url,
        mode="paid",
        http_status=response.status_code,
        checks=checks,
        verdict=summarize(checks),
        curated=curated,
        unpaid_reason=unpaid_reason,
    )


async def _run_settlement_echo_check(
    *,
    url: str,
    accepts: list[dict],
    signer: Any,
    economic_ceiling: EconomicCeiling,
    transport: Optional[Any],
    method: str = "GET",
    json_body: Optional[Any] = None,
) -> tuple[CheckResult, Optional[str]]:
    """The settlement_echo check, plus the reason no test payment was made
    (None when one was attempted, whatever its outcome)."""
    accept = select_payable_accept(accepts)
    if accept is None and any(on_our_rails(a) for a in accepts):
        return _unpaid(
            "The target only offers Permit2 payments, and test payments use the standard "
            "USDC authorization (EIP-3009 transferWithAuthorization) only, so no test "
            "payment was made."
        )
    if accept is None:
        return _unpaid(
            "None of the target's payment options can be paid by Unlisted's test wallet "
            "(scheme exact, network eip155:8453, Base mainnet), so no test payment was made."
        )

    price_usd = usd_price_of_accept(accept)
    if price_usd is None:
        return _unpaid(
            "The target's price couldn't be read in USD (unrecognized asset: test "
            f"payments are only made in USDC on Base mainnet, contract {USDC_BASE_ADDRESS}), "
            "so no test payment was made."
        )

    if not economic_ceiling.price_within_cap(price_usd):
        return _unpaid(
            f"The target's price (${price_usd:.4f}) is above Unlisted's "
            f"${economic_ceiling.per_request_cap:.2f} cap for test payments, so no "
            "test payment was made."
        )

    if not economic_ceiling.can_spend(price_usd):
        return _unpaid(
            "Unlisted's daily budget for test payments is used up, so no test payment "
            "was made. Try paid mode again later."
        )

    try:
        outcome = await attempt_payment(
            url,
            signer,
            max_price_usd=economic_ceiling.per_request_cap,
            price_usd=price_usd,
            transport=transport,
            method=method,
            json_body=json_body,
        )
    except (SSRFBlocked, FetchError):
        # attempt_payment only raises these when no payment was sent.
        return _unpaid(
            "Unlisted couldn't reach the target again to make the test payment, so none was made."
        )

    if not outcome.attempted:
        return _unpaid(outcome.detail)

    if outcome.success:
        economic_ceiling.record_spend(price_usd)

    return _outcome_to_check(outcome), None


def _unpaid(reason: str) -> tuple[CheckResult, str]:
    return _skip(reason), reason


def _skip(detail: str) -> CheckResult:
    return CheckResult(
        check_id="settlement_echo",
        status=Status.SKIP,
        detail=detail,
        confidence=Confidence.CLIENT,
    )


def _outcome_to_check(outcome: PaymentTestOutcome) -> CheckResult:
    if not outcome.attempted:
        return _skip(outcome.detail or outcome.skipped_reason or "Real payment test skipped.")

    if outcome.success:
        paid_note = f" (paid ${outcome.price_usd:.4f})" if outcome.price_usd is not None else ""
        return CheckResult(
            check_id="settlement_echo",
            status=Status.PASS,
            detail=outcome.detail + paid_note,
            confidence=Confidence.FACILITATOR,
        )

    return CheckResult(
        check_id="settlement_echo",
        status=Status.FAIL,
        detail=outcome.detail,
        confidence=Confidence.FACILITATOR,
        fix=(
            "Confirm the facilitator's /verify and /settle responses for this route, "
            "and that settlement actually completes before the resource is returned."
        ),
    )


__all__ = ["run_paid_check"]
