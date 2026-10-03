"""
Unlisted (formerly "x402 Doctor") -- dry-check + paid /diagnose API.

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

import base64
import binascii
import json
import logging
import os
import time
from dataclasses import asdict
from typing import Any, Literal, Optional
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from html import escape

from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel, Field, HttpUrl, model_validator
from x402.http.middleware.fastapi import PaymentMiddlewareASGI

import bazaar
import guide
import outbound_payment
import payment
from dry_check import run_dry_check
from limits import EconomicCeiling, RateLimitExceeded, SlidingWindowRateLimiter, log_submission, log_usage
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


MAX_TARGET_BODY_BYTES = 8_192


class DiagnoseRequest(BaseModel):
    url: HttpUrl = Field(..., description="The x402-protected resource URL to diagnose")
    method: Literal["GET", "POST"] = Field(
        "GET", description="HTTP method the target route uses. Default GET."
    )
    body: Optional[dict[str, Any]] = Field(
        None,
        description="JSON body to send to a POST target (e.g. its example request). POST only.",
    )

    @model_validator(mode="after")
    def _body_only_for_post(self):
        if self.body is not None:
            if self.method != "POST":
                raise ValueError("`body` is only allowed when method is POST")
            if len(json.dumps(self.body)) > MAX_TARGET_BODY_BYTES:
                raise ValueError(f"`body` must be under {MAX_TARGET_BODY_BYTES} bytes as JSON")
        return self


class Mirror402ChallengeMiddleware(BaseHTTPMiddleware):
    """PaymentMiddlewareASGI returns {} as the 402 body and puts the whole
    challenge only in the base64 PAYMENT-REQUIRED header. Many x402 clients
    read accepts[] from the body, so copy the decoded header into it (the
    header is left untouched). Same fix as crypto-sentiment-x402 65de7c2.
    No WWW-Authenticate header: "Payment" there is the MPP auth scheme, which
    needs a server-bound challenge and Authorization: Payment credentials we
    don't accept, and a bare one fails discovery audits (x402scan)."""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if response.status_code != 402:
            return response
        body = b"".join([chunk async for chunk in response.body_iterator])
        header = response.headers.get("payment-required")
        if header and body.strip() in (b"", b"{}"):
            try:
                body = json.dumps(json.loads(base64.b64decode(header))).encode()
            except (ValueError, binascii.Error):
                logger.warning("could not decode PAYMENT-REQUIRED header; leaving 402 body as-is")
        headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        return Response(content=body, status_code=402, headers=headers, media_type="application/json")


def _payer_from_request(request: Request) -> Optional[str]:
    """Best-effort payer address from the x402 v2 PAYMENT-SIGNATURE header
    (payload.authorization.from for the exact EVM scheme). Only used for
    the usage log; never raises."""
    header = request.headers.get("payment-signature") or request.headers.get("x-payment")
    if not header:
        return None
    try:
        data = json.loads(base64.b64decode(header))
        return data.get("payload", {}).get("authorization", {}).get("from")
    except Exception:  # noqa: BLE001
        return None


def _target_domain(url: str) -> str:
    return urlparse(url).hostname or url


# RFC 2606 reserved example domains. Unlisted's marketplace listing shows a
# sample URL on one of these, and callers paste it verbatim -- reject it
# up front with a pointer to what's wrong instead of fetching it.
_EXAMPLE_DOMAINS = ("example.com", "example.org", "example.net")


def _is_example_domain(url: str) -> bool:
    host = (urlparse(url).hostname or "").rstrip(".").lower()
    return any(host == d or host.endswith("." + d) for d in _EXAMPLE_DOMAINS)


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
        title="Unlisted",
        version="0.3.0" if (facilitator_client and pay_to) else "0.3.0-dry-check",
        description=(
            "Find out why your x402 endpoint isn't listed in the Coinbase CDP Bazaar. "
            "Checks CDP's own live index status, and in paid mode makes a real test "
            "payment to your endpoint and reports whether it settled."
        ),
        # x402scan verifies origin ownership via info.contact.email in /openapi.json.
        contact={"email": "hi@unlisted.sh"},
    )

    # Discovery tools (x402scan / @agentcash/discovery) read each operation's
    # auth mode from the spec: x-payment-info marks a paid route, and
    # `security: []` marks one as explicitly public so it isn't probed for a 402.
    _public_op: dict[str, Any] = {"security": []}
    if paywall_active:
        diagnose_openapi: dict[str, Any] = {
            "x-payment-info": {
                "price": {
                    "mode": "dynamic",
                    "currency": "USD",
                    "min": price.lstrip("$"),
                    "max": paid_price.lstrip("$"),
                },
                "protocols": [{"x402": {}}],
            }
        }
    else:
        diagnose_openapi = _public_op

    _default_openapi = fastapi_app.openapi

    def _openapi_with_guidance() -> dict[str, Any]:
        schema = _default_openapi()
        schema["info"]["x-guidance"] = (
            'POST /diagnose with a JSON body {"url": "<your x402 endpoint>"}. '
            "An unpaid request returns HTTP 402; pay with x402 (USDC on Base "
            f"mainnet) and retry. {price} per check, or {paid_price} with "
            "?mode=paid, which also makes one real test payment to the target. "
            "Free guide: https://unlisted.sh/guide"
        )
        return schema

    fastapi_app.openapi = _openapi_with_guidance  # type: ignore[method-assign]

    @fastapi_app.post(
        "/diagnose",
        summary="Diagnose why an x402 endpoint isn't in the CDP Bazaar",
        description=(
            f"Paid per call over x402 in USDC on Base mainnet: {price} per check, "
            f"or {paid_price} with ?mode=paid, which also makes one real test payment "
            "to the target. An unpaid request returns HTTP 402 with the payment "
            "requirements. You are only charged when the check completes."
        ),
        openapi_extra=diagnose_openapi,
        responses={
            402: {
                "description": "Payment required. The requirements are in the "
                "PAYMENT-REQUIRED header and mirrored in the JSON body."
            }
        },
    )
    async def diagnose(payload: DiagnoseRequest, request: Request) -> dict[str, Any]:
        started = time.monotonic()
        url = str(payload.url)
        mode = "paid" if request.query_params.get("mode") == "paid" else "dry"
        # Only passed when non-default, so GET callers (and single-argument
        # test fakes of run_dry_check) see exactly the old call shape.
        target_kwargs: dict[str, Any] = {}
        if payload.method != "GET":
            target_kwargs["method"] = payload.method
        if payload.body is not None:
            target_kwargs["json_body"] = payload.body
        caller = _caller_key(request)
        domain = _target_domain(url)

        if _is_example_domain(url):
            # 400 before any fetch or rate-limit accounting; like every 4xx
            # from this handler, the caller's payment is never settled.
            log_submission(url, blocked=True, reason="example_url", caller=caller)
            raise HTTPException(
                status_code=400,
                detail="That's the sample URL from Unlisted's listing. "
                "Replace it with your own x402 endpoint URL.",
            )

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
                    **target_kwargs,
                )
            elif bazaar_client is not None:
                report = await run_dry_check(url, bazaar_client=bazaar_client, **target_kwargs)
            elif target_kwargs:
                report = await run_dry_check(url, **target_kwargs)
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
        # Top-level answer first: is it in the Bazaar right now? Everything
        # else (verdict, per-check detail) follows unchanged.
        body = asdict(report)
        # `curated` belongs in the bazaar summary, next to index status.
        curated = body.pop("curated", None)
        bazaar_summary = bazaar.summarize_index_status(report.checks, curated)
        log_usage(
            url=url,
            method=payload.method,
            mode=mode,
            paywall_active=paywall_active,
            payer=_payer_from_request(request),
            report=report,
            bazaar_summary=bazaar_summary,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return {
            "bazaar": bazaar_summary,
            **body,
            "method": payload.method,
        }

    @fastapi_app.exception_handler(Exception)
    async def unhandled_exception_handler(request, exc: Exception) -> JSONResponse:
        # Belt-and-suspenders per the spec's resource-exhaustion section: a
        # malformed target response should never crash the service for other
        # users. Everything expected is already caught above; this is the net
        # under it, not the primary error path.
        return JSONResponse(
            status_code=500, content={"detail": "Internal error diagnosing this URL."}
        )

    @fastapi_app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def home() -> HTMLResponse:
        return HTMLResponse(_home_page(price=price, paid_price=paid_price))

    @fastapi_app.get("/guide", response_class=HTMLResponse, include_in_schema=False)
    async def guide_page() -> HTMLResponse:
        return HTMLResponse(guide.guide_page(price=price, paid_price=paid_price))

    for _path, _file, _type in (
        ("/logo.png", "logo.png", "image/png"),
        ("/favicon.ico", "logo.png", "image/png"),
        ("/logo.svg", "logo.svg", "image/svg+xml"),
    ):
        fastapi_app.add_api_route(
            _path, _static_route(_file, _type), methods=["GET"], include_in_schema=False
        )

    @fastapi_app.get("/llms.txt", response_class=PlainTextResponse, include_in_schema=False)
    async def llms_txt() -> PlainTextResponse:
        return PlainTextResponse(guide.llms_txt(price=price, paid_price=paid_price))

    @fastapi_app.get("/diagnose", include_in_schema=False)
    async def diagnose_get_hint() -> JSONResponse:
        # Probes and crawlers often try GET first. Still a 405, but say what to do.
        return JSONResponse(
            status_code=405,
            headers={"Allow": "POST"},
            content={
                "detail": "Use POST. Send JSON like {\"url\": \"https://your-api/paid-route\"}. "
                "An unpaid POST returns HTTP 402 with the payment requirements.",
                "docs": "https://unlisted.sh/llms.txt",
            },
        )

    @fastapi_app.get("/robots.txt", response_class=PlainTextResponse, include_in_schema=False)
    async def robots() -> PlainTextResponse:
        return PlainTextResponse(guide.ROBOTS_TXT)

    @fastapi_app.get("/sitemap.xml", include_in_schema=False)
    async def sitemap() -> Response:
        return Response(content=guide.SITEMAP_XML, media_type="application/xml")

    @fastapi_app.get("/healthz", openapi_extra=_public_op)
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
        # Added after (so it wraps outside) the paywall: sees its 402s.
        fastapi_app.add_middleware(Mirror402ChallengeMiddleware)
    else:
        logger.warning(
            "X402_PAY_TO not set -- running /diagnose without a paywall (dev mode)."
        )

    return fastapi_app


_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def _static_route(filename: str, media_type: str):
    """Serve a small file from ./static (read once at startup), cached for a day."""
    with open(os.path.join(_STATIC_DIR, filename), "rb") as f:
        data = f.read()

    async def _serve() -> Response:
        return Response(
            content=data, media_type=media_type, headers={"Cache-Control": "public, max-age=86400"}
        )

    return _serve


ICON_LINKS = (
    '<link rel="icon" href="/logo.svg" type="image/svg+xml">\n'
    '<link rel="apple-touch-icon" href="/logo.png">\n'
    '<meta property="og:image" content="https://unlisted.sh/logo.png">'
)


def _home_page(*, price: str, paid_price: str) -> str:
    """Static landing page for unlisted.sh. No user input is rendered."""
    price, paid_price = escape(price), escape(paid_price)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Unlisted: why isn't my x402 endpoint in the Bazaar?</title>
<link rel="canonical" href="https://unlisted.sh/">
{ICON_LINKS}
<meta name="description" content="Find out why your x402 endpoint isn't listed in the Coinbase CDP Bazaar, with CDP's live index status and a real test payment.">
<style>
:root {{ --bg:#fafaf9; --fg:#1c1917; --muted:#57534e; --card:#fff; --line:#e7e5e4; --accent:#b45309; --code:#f5f5f4; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0c0a09; --fg:#f5f5f4; --muted:#a8a29e; --card:#1c1917; --line:#292524; --accent:#f59e0b; --code:#292524; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:16px/1.6 system-ui,-apple-system,Segoe UI,sans-serif; }}
main {{ max-width:720px; margin:0 auto; padding:56px 16px 72px; }}
h1 {{ font-size:2.4rem; margin:0 0 4px; letter-spacing:-0.02em; }}
h1 span {{ color:var(--accent); }}
.lede {{ font-size:1.2rem; color:var(--muted); margin:0 0 32px; }}
h2 {{ font-size:1.1rem; margin:36px 0 12px; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px 18px; margin:12px 0; }}
.card b {{ display:block; margin-bottom:2px; }}
.price {{ float:right; color:var(--accent); font-weight:600; }}
pre, code {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:0.88rem; }}
pre {{ background:var(--code); border-radius:8px; padding:14px; overflow-x:auto; }}
code {{ background:var(--code); padding:1px 5px; border-radius:4px; }}
a {{ color:var(--accent); }}
footer {{ margin-top:48px; color:var(--muted); font-size:0.9rem; }}
</style></head>
<body><main>
<h1>unlisted<span>.sh</span></h1>
<p class="lede">Your x402 endpoint works, but it isn't in the Bazaar. Find out why, and get it there.</p>

<p>Want to fix it yourself? Read the free guide: <a href="/guide">Why your x402 endpoint isn't in the CDP Bazaar (and how to fix it)</a>.</p>

<h2>What makes it different</h2>
<div class="card"><b>Asks CDP directly</b>Whether your endpoint is indexed right now, when it was last crawled, and whether CDP's own facilitator would accept it. Ground truth, not a guess.</div>
<div class="card"><b>Makes the first real payment</b>The CDP Bazaar lists a route after CDP's facilitator settles its first payment, so a new endpoint nobody has paid yet stays unlisted. Paid mode pays your endpoint once for real and reports whether it settled end to end.</div>

<h2>Two modes</h2>
<div class="card"><span class="price">{price}</span><b>Check</b>Your 402 challenge and Bazaar declaration, plus CDP's live index status.</div>
<div class="card"><span class="price">{paid_price}</span><b>Check + real payment</b>Everything above, then one real, small test payment to your endpoint (Base mainnet, <code>exact</code> scheme, USDC). Limited to once per domain per 24 hours.</div>

<h2>Call it</h2>
<pre>POST https://unlisted.sh/diagnose
Content-Type: application/json

{{"url": "https://your-api.example.com/paid-route"}}</pre>
<p>For a <code>POST</code> route, add <code>"method": "POST"</code> and, if it needs one, a sample <code>"body"</code>. Add <code>?mode=paid</code> for the real-payment test. Paid per call in USDC on Base via x402: an unpaid request returns HTTP 402 with the payment requirements. The report starts with <code>bazaar.indexed</code>: <code>true</code>, <code>false</code>, or <code>null</code> if it couldn't be checked.</p>

<footer><a href="/guide">Guide: why endpoints go unlisted</a> &middot; <a href="/docs">API docs</a> &middot; <a href="/openapi.json">OpenAPI</a> &middot; <a href="/llms.txt">llms.txt</a> &middot; <a href="/healthz">Status</a></footer>
</main></body></html>"""


app = create_app()
