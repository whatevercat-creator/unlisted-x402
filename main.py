"""
x402 Doctor -- dry-check + paid /diagnose API.

MVP scope per the spec: a single POST /diagnose endpoint, no dashboard, no
history, no signed attestations -- just the diagnosis. This wraps
dry_check.run_dry_check; the only two things that function can raise
(SSRFBlocked, FetchError) become 4xx/502 responses instead of 500s, since
"couldn't check this URL at all" is a different, expected outcome from
"here's what's wrong with your endpoint" -- everything else (bad JSON, a
missing field, a weird status code) is a normal DiagnosisReport, not an
exception.

Inbound paywall (this module's `create_app`, backed by payment.py) charges
$0.02 per call via the CDP facilitator on Base mainnet, using the x402
Python SDK's PaymentMiddlewareASGI. It wraps the whole app, but only
/diagnose is declared in the routes config, so everything else (/healthz)
passes through unpaid -- see x402HTTPResourceServer.requires_payment.

Rate limiting and the economic ceiling stay in place *underneath* the
paywall, not instead of it: the spec's safety section is about protecting
this service and the targets it probes, and payment doesn't substitute for
either (a caller can still pay $0.02 a hundred times a second in a script;
a target domain can still be hammered by many different paying callers).

Paid mode (build-order step 7) adds a real outbound test payment against
the diagnosed target -- see paid_check.py and outbound_payment.py. It's
selected via a `?mode=paid` *query parameter*, not a JSON body field, for a
concrete technical reason, not a style choice: the x402 payment middleware
prices the request before FastAPI ever parses the body (confirmed: the
FastAPI adapter's get_body() always returns None at that point), so a price
that depends on request content has to read something the middleware layer
can actually see -- see payment.py's _price_by_mode. Paid mode costs more
(X402_DOCTOR_PAID_PRICE_USD) and is gated by an extra, much stricter
per-target-domain limit (one real payment test per domain per rolling 24h,
`paid_test_domain_limiter` below) on top of the existing per-caller and
per-target-domain dry-check limits, which still apply to every request
regardless of mode.

Build-order step 9 (the last MVP item) adds an optional live Bazaar
index-status lookup -- see bazaar.py -- to both modes' reports. Unlike
every other optional feature in this file, it needs no CDP credentials at
all (CDP's discovery/validate endpoints are unauthenticated by design; see
bazaar.py's module docstring), so there's no natural "is this configured"
signal the way X402_PAY_TO or X402_DOCTOR_OUTBOUND_TEST_WALLET give the
other features. It's still gated behind its own explicit opt-in env var
(X402_DOCTOR_ENABLE_BAZAAR_LOOKUP) rather than defaulting on, for the same
"deliberately conservative defaults for a not-yet-publicly-launched
service" reason the rate limits above give -- and concretely, so that
every existing test in this codebase that builds an app via create_app()
with no arguments keeps making zero outbound network calls, rather than
every one of them silently gaining a live third-party HTTP request. A real
deployment turns this on once it's ready to depend on CDP's API being
reachable from it.
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict
from typing import Any, Optional
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, HttpUrl
from x402.http.middleware.fastapi import PaymentMiddlewareASGI

import bazaar
import outbound_payment
import payment
from dry_check import run_dry_check
from limits import EconomicCeiling, RateLimitExceeded, SlidingWindowRateLimiter, log_submission
from paid_check import run_paid_check
from safe_fetch import FetchError, SSRFBlocked

logger = logging.getLogger("x402_doctor")

# Defaults are deliberately conservative for a not-yet-publicly-launched
# service; override via env vars once real usage patterns are known.
CALLER_LIMIT = int(os.environ.get("X402_DOCTOR_CALLER_LIMIT_PER_HOUR", "30"))
CALLER_WINDOW_SECONDS = 3600
DOMAIN_LIMIT = int(os.environ.get("X402_DOCTOR_DOMAIN_LIMIT_PER_HOUR", "10"))
DOMAIN_WINDOW_SECONDS = 3600

# Real money leaves the outbound test-payment wallet per successful paid-mode
# check, unlike dry-check -- capped far tighter than the dry-check domain
# limit above: one real payment test per target domain per rolling day,
# regardless of how many different callers ask for it. Checked (and
# recorded) *before* anything else in paid mode, so a blocked repeat-test
# never even reaches the point where the caller's own payment could settle.
PAID_TEST_DOMAIN_LIMIT = int(os.environ.get("X402_DOCTOR_PAID_TEST_DOMAIN_LIMIT_PER_DAY", "1"))
PAID_TEST_DOMAIN_WINDOW_SECONDS = 86400

# Per the spec's economic-ceiling design: this is the safety rail for the
# outbound test-payment wallet (build-order step 7) -- the per-request price
# cap decides whether a target's price is even worth attempting to pay
# (paid_check.py declines above this), and the rolling 24h ceiling is the
# backstop against a determined attacker standing up many cheap endpoints to
# drain the wallet faster than the $0.02/$0.10 diagnosis fees recoup it.
PER_REQUEST_PRICE_CAP = float(os.environ.get("X402_DOCTOR_PER_REQUEST_CAP_USD", "0.05"))
GLOBAL_DAILY_SPEND_CEILING = float(os.environ.get("X402_DOCTOR_DAILY_CEILING_USD", "2.00"))

caller_limiter = SlidingWindowRateLimiter(limit=CALLER_LIMIT, window_seconds=CALLER_WINDOW_SECONDS)
domain_limiter = SlidingWindowRateLimiter(limit=DOMAIN_LIMIT, window_seconds=DOMAIN_WINDOW_SECONDS)
paid_test_domain_limiter = SlidingWindowRateLimiter(
    limit=PAID_TEST_DOMAIN_LIMIT, window_seconds=PAID_TEST_DOMAIN_WINDOW_SECONDS
)
economic_ceiling = EconomicCeiling(
    per_request_cap=PER_REQUEST_PRICE_CAP,
    global_ceiling=GLOBAL_DAILY_SPEND_CEILING,
    window_seconds=86400,
)


class DiagnoseRequest(BaseModel):
    url: HttpUrl = Field(..., description="The x402-protected resource URL to diagnose")


def _target_domain(url: str) -> str:
    return urlparse(url).hostname or url


def _caller_key(request: Request) -> str:
    # Payment is now verified before this handler ever runs (see
    # PaymentMiddlewareASGI in create_app below), so a payer wallet address
    # is technically available via request.state.payment_payload -- but
    # extracting a normalized address from it is scheme-specific (the
    # "exact" EVM scheme nests it inside a signed authorization struct),
    # and getting that wrong would silently weaken this rate limit rather
    # than loudly break it. Staying on caller IP until that extraction is
    # verified against a real payload, rather than guessing at the shape.
    return request.client.host if request.client else "unknown"


def create_app(
    *,
    facilitator_client: Any = None,
    pay_to: Optional[str] = None,
    price: str = payment.DEFAULT_PRICE_USD,
    paid_price: str = payment.DEFAULT_PAID_PRICE_USD,
    outbound_signer: Any = None,
    outbound_wallet_name: Optional[str] = None,
    outbound_transport: Any = None,
    bazaar_client: Any = None,
) -> FastAPI:
    """Build the x402 Doctor FastAPI app.

    Called with no arguments (as this module does at import time, below),
    this mirrors production/dev behavior automatically:
    - If X402_PAY_TO is set, builds the real CDP-backed facilitator client
      and wires the inbound paywall around /diagnose; if not, /diagnose
      stays unpaid, exactly as before paid mode existed. Missing CDP
      credentials with X402_PAY_TO set is not treated as "disable the
      paywall" -- see payment.py's build_live_facilitator_client.
    - If X402_DOCTOR_OUTBOUND_TEST_WALLET is set, `?mode=paid` requests can
      attempt a real outbound test payment via a lazily-built, cached CDP
      signer for that wallet name (built on the *first* paid-mode request
      this process handles, inside the running event loop, since building
      it is async -- see outbound_payment.build_live_signer). If not set,
      `?mode=paid` is rejected with 503 before the caller's own payment is
      ever verified, so nothing is charged for a mode that can't run.

    Called with explicit `facilitator_client`/`pay_to` and/or
    `outbound_signer`/`outbound_transport` (as tests/test_payment.py and
    tests/test_outbound_payment.py do), wires in *exactly* those --  no env
    lookups, no live client, no network access -- so both the inbound
    paywall and the outbound payment flow can be tested against fakes
    without real CDP credentials.

    - If X402_DOCTOR_ENABLE_BAZAAR_LOOKUP is set (to anything other than
      "0"/"false"/"no"), builds a real, unauthenticated bazaar client (see
      bazaar.build_bazaar_client) and both modes' reports gain a live
      Bazaar index-status check. Off by default -- see this module's
      docstring for why. Passing `bazaar_client` explicitly (as
      tests/test_main_bazaar.py does, with a fake) always wins over the
      env var, exactly like the other optional features above.
    """
    used_default_paywall_args = facilitator_client is None and pay_to is None
    if used_default_paywall_args:
        pay_to = os.environ.get("X402_PAY_TO")
        if pay_to:
            facilitator_client = payment.build_live_facilitator_client()

    # Paid mode must never be reachable unless the inbound paywall is also
    # active. Without this, an outbound wallet configured on a deployment
    # that otherwise runs /diagnose unpaid (dev mode, or a paywall outage)
    # would let anyone trigger real outbound spending for free -- the exact
    # inverse of the safety property this whole feature depends on.
    paywall_active = facilitator_client is not None and bool(pay_to)

    used_default_outbound_args = outbound_signer is None and outbound_wallet_name is None
    if used_default_outbound_args:
        outbound_wallet_name = os.environ.get("X402_DOCTOR_OUTBOUND_TEST_WALLET")
    # Building a CDP signer is async, but create_app() itself isn't -- so a
    # live signer is built lazily on the first paid-mode request this app
    # instance handles, and cached here for every request after that.
    _signer_cache: dict[str, Any] = {"signer": outbound_signer}

    async def _get_outbound_signer() -> Any:
        if _signer_cache["signer"] is not None:
            return _signer_cache["signer"]
        if not outbound_wallet_name:
            return None
        _signer_cache["signer"] = await outbound_payment.build_live_signer(outbound_wallet_name)
        return _signer_cache["signer"]

    if bazaar_client is None and os.environ.get(
        "X402_DOCTOR_ENABLE_BAZAAR_LOOKUP", ""
    ).lower() not in ("", "0", "false", "no"):
        bazaar_client = bazaar.build_bazaar_client()

    fastapi_app = FastAPI(
        title="x402 Doctor",
        version="0.2.0-paid-diagnose" if (facilitator_client and pay_to) else "0.1.0-dry-check",
        description="Diagnosis for x402 sellers not showing up in the CDP Bazaar.",
    )

    @fastapi_app.post("/diagnose")
    async def diagnose(payload: DiagnoseRequest, request: Request) -> dict[str, Any]:
        url = str(payload.url)
        mode = "paid" if request.query_params.get("mode") == "paid" else "dry"
        caller = _caller_key(request)
        domain = _target_domain(url)

        try:
            caller_limiter.check_and_record(caller, scope="caller")
            domain_limiter.check_and_record(domain, scope="target_domain")
        except RateLimitExceeded as e:
            log_submission(url, blocked=True, reason=str(e), caller=caller)
            raise HTTPException(
                status_code=429, detail="Rate limit exceeded. Try again later."
            ) from e

        signer = None
        if mode == "paid":
            if not paywall_active:
                # See paywall_active's comment above: paid mode is refused
                # outright when the inbound paywall isn't active, regardless
                # of whether an outbound wallet is configured, so real
                # outbound spending is never reachable for free.
                raise HTTPException(
                    status_code=503,
                    detail="Paid mode (real payment testing) isn't available without an "
                    "active paywall on this deployment. Use the default (dry) mode.",
                )
            signer = await _get_outbound_signer()
            if signer is None:
                # No charge: this raises before the caller's own paid-mode
                # payment is ever verified, so nothing is settled for a mode
                # that can't run on this deployment.
                raise HTTPException(
                    status_code=503,
                    detail="Paid mode (real payment testing) isn't configured on this "
                    "deployment yet. Use the default (dry) mode.",
                )
            try:
                paid_test_domain_limiter.check_and_record(domain, scope="paid_test_domain")
            except RateLimitExceeded as e:
                log_submission(url, blocked=True, reason=str(e), caller=caller)
                raise HTTPException(
                    status_code=429,
                    detail="This target was already real-payment-tested in the last 24 "
                    "hours. Try dry mode, or check back later.",
                ) from e

        try:
            if mode == "paid":
                report = await run_paid_check(
                    url,
                    signer=signer,
                    economic_ceiling=economic_ceiling,
                    transport=outbound_transport,
                    bazaar_client=bazaar_client,
                )
            elif bazaar_client is not None:
                report = await run_dry_check(url, bazaar_client=bazaar_client)
            else:
                # Passing bazaar_client=None explicitly (rather than just
                # omitting it) would break every test in this codebase that
                # monkeypatches main.run_dry_check with a single-argument
                # fake -- omit the kwarg entirely in the (default, and by
                # far most common) case where there's nothing to pass.
                report = await run_dry_check(url)
        except SSRFBlocked as e:
            # Deliberately vague: doesn't confirm *why* it was blocked (private
            # IP vs. redirect vs. something else) to avoid handing back a
            # network-mapping oracle to someone probing internal addresses.
            log_submission(url, blocked=True, reason="ssrf_blocked", caller=caller)
            raise HTTPException(status_code=400, detail="This URL can't be checked.") from e
        except FetchError as e:
            log_submission(url, blocked=False, reason=f"fetch_error: {e}", caller=caller)
            raise HTTPException(status_code=502, detail=f"Could not fetch target: {e}") from e

        log_submission(url, blocked=False, caller=caller)
        return asdict(report)

    @fastapi_app.exception_handler(Exception)
    async def unhandled_exception_handler(request, exc: Exception) -> JSONResponse:
        # Belt-and-suspenders per the spec's resource-exhaustion section: a
        # malformed target response should never crash the service for other
        # users. Everything expected is already caught above; this is the net
        # under it, not the primary error path.
        return JSONResponse(
            status_code=500, content={"detail": "Internal error diagnosing this URL."}
        )

    @fastapi_app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    if facilitator_client is not None and pay_to:
        resource_server = payment.build_resource_server(facilitator_client)
        routes = payment.build_routes(pay_to, price=price, paid_price=paid_price)
        # Wraps the whole app; only routes declared above are actually
        # gated (x402HTTPResourceServer.requires_payment checks the routes
        # dict per-request), so /healthz stays free. Note: PaymentMiddlewareASGI
        # calls x402ResourceServer.initialize() -- a synchronous network call
        # to the facilitator's /supported endpoint -- the first time Starlette
        # builds its middleware stack, which happens lazily on the first
        # request the running process handles, not at import/startup time.
        fastapi_app.add_middleware(PaymentMiddlewareASGI, routes=routes, server=resource_server)
    else:
        logger.warning(
            "X402_PAY_TO not set -- running /diagnose without a paywall (dev mode)."
        )

    return fastapi_app


app = create_app()
