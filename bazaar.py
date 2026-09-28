"""
Bazaar catalog lookup -- build-order step 9, the last item in the MVP list.

Every check so far (diagnosis.py's rule chain, paid_check.py's settlement
echo) is x402 Doctor's own *inference* about what should make an endpoint
indexable. This module instead asks CDP directly: is this URL actually in
the Bazaar catalog right now, and if not, would CDP's own facilitator even
accept it? That's a materially different, more authoritative signal than
anything the rest of this codebase can produce on its own -- CDP is the
thing doing the indexing, so this is ground truth instead of inference.

WHICH ENDPOINT, AND WHY: the CDP SDK's generated OpenAPI client
(cdp.openapi_client.api.x402_facilitator_api.X402FacilitatorApi) exposes
four Bazaar-related operations: list_x402_discovery_merchant (by payTo),
list_x402_discovery_resources (by protocol type), search_x402_resources
(full search), and validate_x402_resource. This module uses only
validate_x402_resource -- confirmed via its own docstring (installed
cdp-sdk==1.48.1) to be the single richest call: "Validates an x402
endpoint's bazaar-discovery configuration by probing the seller's URL
live. Returns a uniform array of preflight check results (reachable,
returns402, hasBazaarExtension, parse) and a simulated facilitator
accept/reject decision ... This operation is read-only: it performs no
payment and does not index the resource." Its response
(X402ValidateResponse) includes an `index` field (X402ValidateIndex:
active, last_crawled_at, quality) that is exactly "is this indexed in
Bazaar right now" -- the question this whole product exists to answer --
plus `simulation` (would CDP's facilitator accept/reject it) and
`preflight` (CDP's own equivalent of this project's dry-check rule chain).
The other three endpoints are useful for browsing/searching the catalog
but don't add anything validate_x402_resource doesn't already cover for a
single-URL diagnosis, so they're left unused for this MVP.

UNAUTHENTICATED, CONFIRMED TWO WAYS: (1) CdpClient.__init__'s own
docstring: "The CDP Secret API Key is not required to call public
(unauthenticated) endpoints, such as the x402 Bazaar discovery endpoints.
A CdpClient may be constructed without credentials for this purpose." (2)
cdp.openapi_client.public_operations.PUBLIC_OPERATIONS explicitly lists
POST /v2/x402/validate and all four GET /v2/x402/discovery/* paths --
that's the exact table CdpApiClient's request-signing logic consults to
decide whether to attach CDP auth headers at all. So build_bazaar_client()
below constructs a CdpClient with no credentials, and never reads
CDP_API_KEY_ID/CDP_API_KEY_SECRET from the environment -- this is by
design, not an oversight: those credentials are for the *inbound*
facilitator client (payment.py) and the *outbound* wallet signer
(outbound_payment.py), an entirely separate concern from this read-only,
public lookup.

NO HIGH-LEVEL WRAPPER EXISTS: CdpClient exposes .evm/.solana/.policies/
.end_user/.webhooks properties (checked cdp/api_clients.py) but nothing
for x402_facilitator_api -- it isn't wired into ApiClients at all. So
X402FacilitatorApi is constructed directly here, handed the CdpClient's
own underlying low-level api_client (the thing that actually carries the
base URL, retry config, and public/authenticated request-signing logic),
rather than reimplementing any of that against raw httpx.

VERIFICATION: the exact REST paths, query/body parameters, and response
model field names above were confirmed by reading the installed SDK's
generated source directly (the `_..._serialize` methods and each
response model's `model_fields`), not assumed from documentation. A live
GET was also made against /v2/x402/discovery/merchant from this sandbox
(this environment's outbound network policy allows the WebFetch tool to
reach api.cdp.coinbase.com but blocks a raw outbound curl/httpx call to
the same host at the shell level -- confirmed via the agent proxy's own
status endpoint, which logged a 403 policy denial for exactly that host),
which returned a real, live response matching the shape read from source.
validate_x402_resource itself is POST-with-body, which WebFetch can't
issue, so its response shape here is verified from the SDK's own
generated request/response code and its "read-only, no payment, no
indexing side effect" docstring guarantee, rather than a live call.

SAFETY / FAILURE MODE: this is a best-effort enhancement to a diagnosis,
not a dependency the rest of the report should ever fail on. Any error
talking to CDP (network failure, timeout, a non-2xx ApiException, or
CDP being down) is caught here and turned into a SKIP CheckResult with
the error folded into `detail` -- never raised past this module. A buyer
who already paid for a diagnosis should still get everything else in the
report even if CDP's own API happens to be unreachable at that moment.
"""

from __future__ import annotations

from typing import Any, Optional

from diagnosis import CheckResult, Confidence, Status

CHECK_ID = "bazaar_index_status"


def build_bazaar_client() -> Any:
    """Build a reusable, unauthenticated X402FacilitatorApi client for
    Bazaar lookups. Safe to call with no CDP credentials configured at all
    (see module docstring) -- meant to be built once (e.g. at app startup,
    the same lifecycle as payment.py's build_live_facilitator_client) and
    reused across requests, not rebuilt per call.

    Imports the cdp package lazily, same reasoning as
    payment.py/outbound_payment.py: keeps this module importable in any
    environment that doesn't have the (optional, if this feature is ever
    split out) CDP SDK installed, and keeps its import-time notice print
    from firing unless this path is actually used.
    """
    from cdp import CdpClient
    from cdp.openapi_client.api.x402_facilitator_api import X402FacilitatorApi

    cdp_client = CdpClient()  # no credentials -- see module docstring
    return X402FacilitatorApi(api_client=cdp_client.cdp_api_client)


async def check_bazaar_index_status(url: str, bazaar_client: Optional[Any]) -> Optional[CheckResult]:
    """Ask CDP whether `url` is currently indexed in the Bazaar catalog,
    via a single validate_x402_resource call, and turn the result into a
    CheckResult.

    Returns None (rather than a CheckResult) when `bazaar_client` is None
    -- the signal for "this feature isn't configured on this deployment,"
    distinct from every other outcome (indexed, not-yet-indexed-but-would-
    pass, rejected, or couldn't-reach-CDP), which is why callers append
    this check conditionally instead of always. That lets dry_check.py and
    paid_check.py stay pure functions of their existing dependencies for
    every caller that doesn't pass a bazaar_client (in particular, the
    entire existing test suite), rather than needing a fake in every test.
    """
    if bazaar_client is None:
        return None

    if not url.startswith("https://"):
        # Confirmed empirically (constructing a real X402ValidateRequest
        # against an http:// URL): CDP's validate endpoint enforces
        # `resource` against `^https://.*$` and rejects anything else with
        # a pydantic ValidationError. Checked here explicitly, before ever
        # touching the SDK's request model, so the SKIP reason is a clear
        # sentence instead of a raw regex-validator error message leaking
        # into a diagnosis report.
        return CheckResult(
            check_id=CHECK_ID,
            status=Status.SKIP,
            detail=(
                "CDP's Bazaar validation API only accepts https:// resource URLs; "
                "this target was reached over a non-https URL, so live index-status "
                "can't be checked this way."
            ),
            confidence=Confidence.CLIENT,
        )

    from cdp.openapi_client.exceptions import ApiException
    from cdp.openapi_client.models.x402_validate_request import X402ValidateRequest

    try:
        response = await bazaar_client.validate_x402_resource(
            X402ValidateRequest(resource=url, method="GET")
        )
    except ApiException as e:
        return CheckResult(
            check_id=CHECK_ID,
            status=Status.SKIP,
            detail=f"Couldn't reach CDP's Bazaar validation API (HTTP {e.status}: {e.reason}) -- skipping live index-status lookup.",
            confidence=Confidence.CLIENT,
        )
    except Exception as e:  # noqa: BLE001 -- CDP being unreachable must never fail the whole diagnosis
        return CheckResult(
            check_id=CHECK_ID,
            status=Status.SKIP,
            detail=f"Couldn't reach CDP's Bazaar validation API ({e}) -- skipping live index-status lookup.",
            confidence=Confidence.CLIENT,
        )

    return _result_from_validate_response(response)


def _result_from_validate_response(response: Any) -> CheckResult:
    index = getattr(response, "index", None)
    simulation = getattr(response, "simulation", None)
    preflight = getattr(response, "preflight", None) or []

    indexed = bool(index and getattr(index, "active", False))

    if indexed:
        detail = "Currently indexed in the CDP Bazaar catalog."
        last_crawled = getattr(index, "last_crawled_at", None)
        if last_crawled:
            detail += f" Last crawled {last_crawled}."
        quality = getattr(index, "quality", None)
        if quality is not None:
            calls = getattr(quality, "l30_days_total_calls", None)
            payers = getattr(quality, "l30_days_unique_payers", None)
            if calls is not None or payers is not None:
                detail += (
                    f" {calls or 0} call(s) / {payers or 0} unique payer(s) "
                    "in the last 30 days."
                )
        return CheckResult(
            check_id=CHECK_ID,
            status=Status.PASS,
            detail=detail,
            confidence=Confidence.FACILITATOR,
        )

    outcome = getattr(simulation, "outcome", None) if simulation else None
    failing_preflight = [c for c in preflight if not getattr(c, "passed", True)]

    if outcome == "accepted":
        return CheckResult(
            check_id=CHECK_ID,
            status=Status.WARN,
            detail=(
                "Not yet indexed in the CDP Bazaar catalog, but CDP's own simulated "
                "facilitator check says this endpoint WOULD be accepted for indexing -- "
                "this looks like crawl lag or a not-yet-crawled resource, not a "
                "configuration problem."
            ),
            confidence=Confidence.FACILITATOR,
            fix=(
                "Wait for Bazaar's next crawl. If it still isn't indexed after that, "
                "the usual cause is that no payment has been settled for it yet: the "
                "Bazaar lists a route after the facilitator settles its first payment. "
                "Re-run with ?mode=paid and Unlisted will make that first real, small "
                "payment (Base mainnet, exact scheme) and report whether it settled."
            ),
        )

    reasons = []
    rejection_reason = getattr(simulation, "rejection_reason", None) if simulation else None
    if rejection_reason:
        reasons.append(rejection_reason)
    for check in failing_preflight:
        label = getattr(check, "check", None) or "check"
        detail_text = getattr(check, "detail", None)
        reasons.append(f"{label}: {detail_text}" if detail_text else label)

    detail = "Not indexed in the CDP Bazaar catalog."
    if outcome == "rejected":
        detail += " CDP's own facilitator simulation says it would be rejected."
    if reasons:
        detail += " " + "; ".join(reasons)

    return CheckResult(
        check_id=CHECK_ID,
        status=Status.FAIL,
        detail=detail,
        confidence=Confidence.FACILITATOR,
        fix=(
            "Fix the issue(s) CDP's own preflight/simulation reported above, then "
            "re-run this check -- this is CDP's own facilitator decision, not this "
            "tool's inference, so it's the most authoritative signal available for "
            "why this endpoint isn't showing up in Bazaar."
        ),
    )


# Top-level answer to "is this endpoint in the Bazaar right now?", derived
# from the bazaar_index_status check so callers don't have to dig through
# checks[] for the one line the product is about.
_INDEX_STATUS_BY_CHECK = {
    Status.PASS: ("indexed", True),
    Status.WARN: ("not_indexed_would_be_accepted", False),
    Status.FAIL: ("not_indexed", False),
    Status.SKIP: ("unknown", None),
}


def summarize_index_status(checks: list[CheckResult]) -> dict[str, Any]:
    """Return {"indexed": bool|None, "status": str, "detail": str} for the
    report's top-level `bazaar` field. indexed is None when the live lookup
    didn't run (feature off, non-https target, or CDP unreachable)."""
    for check in checks:
        if check.check_id == CHECK_ID:
            status, indexed = _INDEX_STATUS_BY_CHECK.get(check.status, ("unknown", None))
            return {"indexed": indexed, "status": status, "detail": check.detail}
    return {
        "indexed": None,
        "status": "unknown",
        "detail": "Live Bazaar index lookup is not enabled on this deployment.",
    }


__all__ = ["build_bazaar_client", "check_bazaar_index_status", "summarize_index_status", "CHECK_ID"]
