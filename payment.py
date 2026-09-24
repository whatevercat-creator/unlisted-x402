"""
Inbound paywall for x402 Doctor's own /diagnose endpoint.

Per the spec's tech stack: FastAPI + the x402 Python SDK, charging a flat
$0.02 per diagnosis via the CDP facilitator, on Base mainnet -- not
testnet, because the whole premise of this product is dogfooding the exact
payment flow real x402 buyers use, and a paywall real buyers can't actually
pay against would defeat that.

Kept in its own module, separate from main.py, for one reason:
x402ResourceServer.initialize() -- which x402's own PaymentMiddlewareASGI
calls synchronously the moment it's constructed -- makes a *real, blocking*
network call (facilitator_client.get_supported()) to learn what the
facilitator supports. A misconfigured or unreachable facilitator is
*meant* to fail fast (see x402's payment_middleware() docstring/comments:
"a misconfigured server [should not] stay up until the first paid
request"), which is the right behavior in production. But it means the
concrete facilitator client can't be hardcoded at import time without
making every test -- and every dev environment without CDP credentials --
unable to even import this service.

So: this module only builds the *pieces* (routes, resource server, the
real CDP-backed client). main.py decides at app-construction time whether
to wire them in, based on whether X402_PAY_TO is configured. Tests build
their own app via main.create_app(), handing in a fake FacilitatorClient
(see tests/test_payment.py) that structurally satisfies the
verify/settle/get_supported protocol x402ResourceServer actually needs
(x402.http.facilitator_client_base.FacilitatorClient) without subclassing
anything or touching the network -- so the real middleware code path
(header parsing, challenge building, verify-then-settle sequencing) gets
exercised for real, just against a fake facilitator instead of CDP.

One behavior worth calling out because it's easy to miss and it matters
for cost safety: verify() happens *before* the route handler runs, but
settle() -- the step that actually moves funds -- only happens *after* the
handler returns a response with status < 400 (see x402's fastapi
middleware: "Don't settle on error responses"). That means a request that
gets rate-limited or hits a fetch error inside /diagnose (both of which
raise HTTPException with 4xx/5xx status from *inside* the handler, after
payment was verified but before this module is involved at all) never
gets settled -- the buyer isn't charged for a diagnosis they didn't get.
Confirmed empirically against the installed x402 SDK (v2.24.0), not just
assumed from reading the docstring -- see the exploration this module's
tests are built on.

A second behavior worth calling out, found *the hard way* while building
the outbound test-payment side of this project (paid_check.py /
outbound_payment.py) and worth knowing about here too, since the mechanism
is identical: x402HTTPResourceServer.initialize() -- called by
x402ResourceServer.initialize() during the same startup sequence described
above -- treats a facilitator/route capability mismatch (the facilitator's
/supported response doesn't list the scheme+network a route requires) as
*fatal*, and reports it by calling os._exit(1) directly. That's an
unconditional, immediate process kill -- not a raisable Python exception,
not something a try/except anywhere in this codebase can catch, and (found
while writing this project's own tests) not something that even leaves a
traceback: it silently ends the process it's called in. For OUR OWN route
(this module's build_routes/build_resource_server, registered for exactly
"exact" on "eip155:8453") this should only ever fire if CDP's real
facilitator stops reporting support for its own mainnet exact scheme --
extremely unlikely, but not impossible (a CDP-side outage or bug), and the
practical consequence would be x402 Doctor's entire server process dying on
the first /diagnose request after that happens, not a clean 503. Accepted
as a known risk for now rather than worked around (there's no supported way
to intercept it), but worth monitoring for and revisiting if CDP facilitator
health ever becomes a real operational concern.
"""

from __future__ import annotations

import os

from x402.http.types import PaymentOption, RouteConfig, RoutesConfig
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.server import x402ResourceServer

# Base mainnet CAIP-2 identifier -- this is what CDP Bazaar indexes against
# and what a real x402 buyer pays on. eip155:84532 would be Base Sepolia
# (testnet) if this ever needs a non-production mode.
NETWORK = "eip155:8453"
SCHEME = "exact"

# $0.02 per the spec. Kept as a plain price string ("$0.02") rather than a
# pre-computed atomic amount -- the SDK's money parser resolves this against
# the network's USDC contract and decimals itself (confirmed: it turns
# "$0.02" into "20000" -- USDC has 6 decimals -- and fills in the Base
# mainnet USDC asset address automatically). Overridable via env var so the
# price can change without a code deploy.
DEFAULT_PRICE_USD = os.environ.get("X402_DOCTOR_PRICE_USD", "$0.02")

# Paid mode (?mode=paid -- see build_routes below) costs more than dry mode:
# it covers a real outbound test payment against the target plus its own
# gas/facilitator overhead, on top of the base diagnosis fee. $0.10 is a
# starting number, not a researched one -- easy to retune via env var once
# real outbound-payment costs are observed.
DEFAULT_PAID_PRICE_USD = os.environ.get("X402_DOCTOR_PAID_PRICE_USD", "$0.10")

# Bazaar discovery metadata for x402 Doctor's *own* listing. Deliberately
# kept within the caps RouteConfig's docstring documents (service_name <=32
# chars, <=5 tags of <=32 chars each) -- getting soft-dropped from our own
# listing for exceeding a limit this whole product exists to catch in other
# people's endpoints would be a fairly embarrassing bug.
SERVICE_NAME = "x402 Doctor"
TAGS = ["x402", "diagnostics", "bazaar"]
DESCRIPTION = (
    "Diagnose why an x402-protected endpoint isn't showing up in the "
    "Coinbase CDP Bazaar discovery catalog."
)


def _price_by_mode(dry_price: str, paid_price: str):
    """A DynamicPrice callable choosing dry vs. paid pricing from a
    `?mode=paid` query parameter on the incoming request.

    Query parameter, not a JSON body field, and this isn't a style
    preference -- confirmed empirically against the installed FastAPI
    adapter (x402.http.middleware.fastapi.FastAPIAdapter.get_body always
    returns None; the payment layer runs as ASGI middleware, before
    FastAPI's own request-body parsing, so there's no body for it to read
    yet). get_query_param, by contrast, reads request.query_params
    directly and works exactly as you'd expect -- verified by watching
    which one actually changed the amount in a real 402 challenge before
    this was written this way instead of a request-body field.
    """

    def _price(ctx) -> str:
        mode = ctx.adapter.get_query_param("mode")
        return paid_price if mode == "paid" else dry_price

    return _price


def build_routes(
    pay_to: str,
    price: str = DEFAULT_PRICE_USD,
    paid_price: str = DEFAULT_PAID_PRICE_USD,
) -> RoutesConfig:
    """The one protected route: POST /diagnose, priced by `?mode=paid` vs.
    the default (dry) mode, both payable to `pay_to` on Base mainnet.
    `pay_to` should be x402 Doctor's own dedicated wallet address -- not
    crypto-sentiment-x402's or any other seller's -- per the earlier
    resolved decision to keep this service's revenue wallet separate from
    any seller it diagnoses. This is the *inbound* wallet (what buyers pay
    x402 Doctor); the outbound test-payment wallet (what x402 Doctor pays
    a diagnosed target with) is entirely separate -- see outbound_payment.py.
    """
    return {
        "POST /diagnose": RouteConfig(
            accepts=PaymentOption(
                scheme=SCHEME,
                pay_to=pay_to,
                price=_price_by_mode(price, paid_price),
                network=NETWORK,
            ),
            description=DESCRIPTION,
            service_name=SERVICE_NAME,
            tags=TAGS,
        )
    }


def build_resource_server(facilitator_client) -> x402ResourceServer:
    """Wires one facilitator client to the one scheme/network this service
    accepts payment on. `facilitator_client` is a parameter rather than
    built here specifically so tests can hand in a fake -- anything that
    structurally satisfies FacilitatorClient's verify/settle/get_supported
    (x402.http.facilitator_client_base.FacilitatorClient is a Protocol, not
    a base class to subclass) works here, including the real
    HTTPFacilitatorClient from build_live_facilitator_client() below.
    """
    server = x402ResourceServer(facilitator_client)
    server.register(NETWORK, ExactEvmServerScheme())
    return server


def build_live_facilitator_client():
    """The real CDP-backed facilitator client, for production use only --
    importing cdp.x402 here (rather than at module top) keeps this module
    importable in any environment that has the x402 SDK but not the CDP
    SDK, and keeps the CDP package's own import-time notice print (about
    Bazaar search ToS) from firing unless this path is actually used.

    Reads CDP_API_KEY_ID / CDP_API_KEY_SECRET from the environment via
    create_facilitator_config(). Both should be set for any real
    deployment: CDP's facilitator has historically required authenticated
    requests for verify/settle (unauthenticated access is meant for the
    Bazaar list endpoint, not for moving money) -- this isn't re-verified
    here since doing so would require live credentials this sandbox
    doesn't have, so treat missing credentials as a deploy-time
    misconfiguration to fix, not a supported "anonymous" mode.
    """
    from cdp.x402 import create_facilitator_config
    from x402.http import HTTPFacilitatorClient

    return HTTPFacilitatorClient(create_facilitator_config())
