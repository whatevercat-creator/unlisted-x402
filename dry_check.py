"""
Dry-check mode for x402 Doctor: fetch a target's 402 challenge and run
checks 1-4 against it, with no real payment made. This is the free tier
from the spec's pricing section, and MVP build-order step 2.

Build-order step 9 adds an optional extra check on top: a live Bazaar
index-status lookup via bazaar.check_bazaar_index_status, appended after
the ordinary rule-chain checks exactly like paid_check.py appends its
settlement_echo check. `bazaar_client` is None by default here (and this
module never builds one itself) so every existing caller -- in particular
the whole existing test suite -- is completely unaffected; main.py is
what decides whether to pass a real one, gated behind its own env var per
that module's comments.
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
    run_checks,
    summarize,
)
from probe import check_probe_response
from safe_fetch import FetchError, SSRFBlocked, safe_fetch


async def run_dry_check(
    url: str,
    *,
    bazaar_client: Optional[Any] = None,
    method: str = "GET",
    json_body: Optional[Any] = None,
) -> DiagnosisReport:
    """Fetch `url` the SSRF-safe way and diagnose the 402 challenge it
    returns. Never raises for an ordinary "this endpoint has problems"
    outcome -- SSRFBlocked and FetchError are the only two ways this can
    still raise, and both mean "we couldn't check this at all," which
    callers (the future /diagnose endpoint) should turn into a 4xx rather
    than a diagnosis report.
    """
    response = await safe_fetch(url, method=method, **_body_kwargs(json_body))

    if response.status_code != 402:
        return DiagnosisReport(
            url=url,
            mode="dry",
            http_status=response.status_code,
            checks=[],
            verdict=(
                f"Expected HTTP 402, got {response.status_code}. "
                + (
                    "This endpoint may not require payment at all, or may not be "
                    "an x402 seller."
                    if response.status_code == 200
                    else "Can't run the payment-requirement checks without a 402 "
                    "challenge to inspect."
                )
            ),
        )

    try:
        # x402Version 2 puts the actual challenge in a base64-encoded
        # `payment-required` header (confirmed against a real deployment,
        # where the body itself is just `{}`); older/body-only sellers
        # still get parsed from the body as a fallback.
        challenge = decode_challenge(response.headers.get("payment-required"), response.body)
    except ChallengeParseError as e:
        return DiagnosisReport(
            url=url,
            mode="dry",
            http_status=response.status_code,
            checks=[],
            verdict="Could not parse the 402 response as an x402 challenge.",
            parse_error=str(e),
        )

    checks = run_checks(challenge, final_url=response.url)
    # Looked up at call time so tests that monkeypatch safe_fetch cover it too.
    checks.append(await check_probe_response(challenge, response.url, safe_fetch, method))

    bazaar_check = await check_bazaar_index_status(response.url, bazaar_client, method)
    if bazaar_check is not None:
        checks.append(bazaar_check)
    curated = await lookup_curated(challenge, response.url, bazaar_client, bazaar_check)

    return DiagnosisReport(
        url=url,
        mode="dry",
        http_status=response.status_code,
        checks=checks,
        verdict=summarize(checks),
        curated=curated,
    )


def _body_kwargs(json_body: Optional[Any]) -> dict:
    """safe_fetch kwargs for an optional JSON request body."""
    if json_body is None:
        return {}
    import json

    return {
        "content": json.dumps(json_body).encode(),
        "headers": {"content-type": "application/json"},
    }


__all__ = ["run_dry_check", "DiagnosisReport", "CheckResult", "Status", "Confidence", "SSRFBlocked", "FetchError"]
