"""
Diagnosis engine for x402 Doctor.

A rule chain, not a model, per the spec: each check is a small, independent
function over the parsed 402 challenge (and, for check 1, the URL the
challenge actually came from). Nothing here does network I/O -- callers
fetch the challenge with safe_fetch and hand the parsed JSON in.

CONFIRMED SCHEMA (September 24, 2026, via a real `curl -i` against
crypto-sentiment-x402.onrender.com/sentiment/BTC -- the reference
implementation this checklist was built from):

    HTTP/2 402
    content-type: application/json
    payment-required: <base64-encoded JSON, decoded below>

    {}                                          <- body is genuinely empty

    # base64-decoded `payment-required` header value:
    {
      "x402Version": 2,
      "error": "Payment required",
      "resource": {
        "url": "https://.../sentiment/BTC",
        "description": "...",
        "mimeType": "application/json"
      },
      "accepts": [
        {
          "scheme": "exact", "network": "eip155:8453",
          "asset": "0x...", "amount": "10000", "payTo": "0x...",
          "maxTimeoutSeconds": 300, "extra": {"name": "USD Coin", "version": "2"}
        }
      ],
      "extensions": {
        "bazaar": {
          "info": {"input": {...}, "output": {...}},
          "schema": {...},
          "routeTemplate": "/sentiment/:symbol"
        }
      }
    }

This settles the schema ambiguity from the prior pass in a way neither
candidate fully predicted: it IS the x402Version 2 shape docs.x402.org
described as header-based (base64 in a `payment-required` header, CAIP-2
network ids, `amount` not `maxAmountRequired`) -- but the *inner* JSON
still carries `resource`/`extensions` directly, much like the body-shaped
version. Critically, `resource`, `description` (nested under `resource`,
not top-level), and `extensions` all live at the CHALLENGE level, shared
across every entry in `accepts` -- not duplicated per-accept the way the
original (pre-verification) version of this module assumed. That was a
real bug: it would have looked for `resource`/`description`/`extensions`
on each accepts[] entry, found nothing, and reported false failures
against a completely healthy endpoint.

`extensions.bazaar.routeTemplate` was also a surprise -- not documented at
docs.x402.org, but present in this real payload (and it directly answers
the original checklist's check 5, wildcard-vs-named-params, when present).
It's treated as opportunistic here: used when present, skipped when not,
since it isn't part of any spec this module could confirm.

Field lookups below still fall back to the old per-accept convention when
the challenge-level field is absent, since a different seller's
implementation (an older x402 v1 library, or a body-only responder) could
still use it -- degrading to "field not found" rather than crashing on a
shape this hasn't seen yet. But one real, verified example is worth more
than documentation that gave contradictory answers, so the challenge-level
reading is now the primary path, not the fallback.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional
from urllib.parse import urlparse


class Status(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    SKIP = "skip"


class Confidence(str, Enum):
    CLIENT = "client-side"
    SERVER = "server-side"
    FACILITATOR = "facilitator-side"
    UNKNOWN = "unknown"


@dataclass
class CheckResult:
    check_id: str
    status: Status
    detail: str
    fix: Optional[str] = None
    confidence: Confidence = Confidence.UNKNOWN


@dataclass
class DiagnosisReport:
    url: str
    mode: str  # "dry" | "paid"
    http_status: Optional[int]
    checks: list[CheckResult] = field(default_factory=list)
    verdict: str = ""
    parse_error: Optional[str] = None

    @property
    def failures(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status == Status.FAIL]


class ChallengeParseError(Exception):
    """Raised when the response body can't be interpreted as a 402
    challenge at all (not JSON, or JSON with no usable payment-requirement
    shape). Distinct from an individual check failing -- this means we
    couldn't run the checks in the first place."""


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def parse_challenge(body: bytes) -> dict[str, Any]:
    """Parse the 402 response body as JSON. Raises ChallengeParseError on
    anything that isn't a JSON object -- deliberately not swallowed into a
    generic check failure, since every other check depends on this having
    worked."""
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ChallengeParseError(f"402 response body is not valid JSON: {e}") from e

    if not isinstance(data, dict):
        raise ChallengeParseError(
            f"402 response body is valid JSON but not an object (got {type(data).__name__})"
        )
    return data


def decode_challenge(header_value: Optional[str], body: bytes) -> dict[str, Any]:
    """Decode the 402 challenge, preferring the `payment-required` header
    (x402Version 2: base64-encoded JSON) over the response body.

    Confirmed against a real deployment that the body is genuinely `{}` in
    this shape -- the payment requirements live entirely in the header.
    Falls back to parsing the body as JSON when the header is absent or
    doesn't decode cleanly, for sellers running an older body-only shape.
    """
    if header_value:
        try:
            padded = header_value + "=" * (-len(header_value) % 4)
            decoded_bytes = base64.b64decode(padded, validate=False)
            data = json.loads(decoded_bytes)
            if isinstance(data, dict):
                return data
        except (binascii.Error, ValueError, json.JSONDecodeError, UnicodeDecodeError):
            pass  # fall through to body parsing -- a malformed header isn't fatal
    return parse_challenge(body)


def extract_accepts(challenge: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the list of payment-requirement objects to check.

    Prefers the documented `accepts` array. Falls back to treating the
    whole challenge object as a single accept-like entry when `accepts` is
    missing or empty, so a schema variant that flattens the structure still
    gets *something* checked rather than nothing -- see the module
    docstring's schema caveat for why this defensiveness exists.
    """
    accepts = challenge.get("accepts")
    if isinstance(accepts, list) and accepts:
        return [a for a in accepts if isinstance(a, dict)]
    return [challenge]


def _resource_value(challenge: dict[str, Any], accepts: list[dict[str, Any]]) -> Any:
    """`resource` lives at the challenge level in the confirmed real shape;
    fall back to the first accepts[] entry for the older per-accept
    convention, in case some seller's implementation still uses it."""
    resource = challenge.get("resource")
    if resource is not None:
        return resource
    if accepts:
        return accepts[0].get("resource")
    return None


def _resource_url(resource: Any) -> Optional[str]:
    """`resource` has been observed as either a bare URL string or an
    object with a `url` field (the confirmed real shape); handle both."""
    if isinstance(resource, str):
        return resource
    if isinstance(resource, dict):
        url = resource.get("url")
        return url if isinstance(url, str) else None
    return None


def _extensions_value(challenge: dict[str, Any], accepts: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """`extensions` lives at the challenge level in the confirmed real
    shape; fall back to the first accepts[] entry otherwise."""
    extensions = challenge.get("extensions")
    if isinstance(extensions, dict):
        return extensions
    if accepts and isinstance(accepts[0].get("extensions"), dict):
        return accepts[0]["extensions"]
    return None


def _description_value(
    challenge: dict[str, Any], resource: Any, accepts: list[dict[str, Any]]
) -> Optional[str]:
    """`description` is nested under `resource.description` in the
    confirmed real shape. Falls back to a top-level `description` or a
    per-accept one for schema variance."""
    if isinstance(resource, dict) and isinstance(resource.get("description"), str):
        return resource["description"]
    if isinstance(challenge.get("description"), str):
        return challenge["description"]
    if accepts and isinstance(accepts[0].get("description"), str):
        return accepts[0]["description"]
    return None


# --------------------------------------------------------------------------
# Checks 1-4 (all answerable from a single challenge response -- dry-check)
# --------------------------------------------------------------------------


def check_scheme_mismatch(
    challenge: dict[str, Any], accepts: list[dict[str, Any]], final_url: str
) -> CheckResult:
    """Check 1: does the challenge's advertised `resource` scheme
    (http/https) match the scheme the challenge actually arrived over?
    A mismatch is the classic symptom of a reverse proxy terminating TLS
    without the app knowing to advertise https."""
    resource = _resource_value(challenge, accepts)
    resource_url = _resource_url(resource)
    if not resource_url:
        return CheckResult(
            check_id="scheme_mismatch",
            status=Status.SKIP,
            detail="No `resource` URL to compare against (see resource_present check)",
        )

    resource_scheme = urlparse(resource_url).scheme.lower()
    actual_scheme = urlparse(final_url).scheme.lower()

    if not resource_scheme:
        return CheckResult(
            check_id="scheme_mismatch",
            status=Status.SKIP,
            detail=f"Could not parse a scheme out of resource URL {resource_url!r}",
        )

    if resource_scheme != actual_scheme:
        return CheckResult(
            check_id="scheme_mismatch",
            status=Status.FAIL,
            detail=(
                f"Challenge advertises resource scheme {resource_scheme!r} but the "
                f"challenge was actually served over {actual_scheme!r}"
            ),
            fix=(
                "Behind a reverse proxy/load balancer that terminates TLS, the app "
                "sees http:// internally even though clients connect over https://. "
                "Build the advertised resource URL from a trusted forwarded-proto "
                "header (e.g. `X-Forwarded-Proto`) instead of the request's own "
                "scheme, or hardcode the canonical public scheme if there's only one."
            ),
            confidence=Confidence.SERVER,
        )

    return CheckResult(
        check_id="scheme_mismatch",
        status=Status.PASS,
        detail=f"Resource scheme ({resource_scheme}) matches how the challenge was served",
        confidence=Confidence.SERVER,
    )


def check_resource_present(
    challenge: dict[str, Any], accepts: list[dict[str, Any]]
) -> CheckResult:
    """Check 2: is `resource` present at all?"""
    resource = _resource_value(challenge, accepts)
    resource_url = _resource_url(resource)
    if not resource_url:
        return CheckResult(
            check_id="resource_present",
            status=Status.FAIL,
            detail="`resource` field is missing or empty on the payment requirement",
            fix=(
                "Add a `resource` field (the fully-qualified URL of the paid "
                "endpoint) to the payment requirement object returned in the 402 "
                "body -- Bazaar needs this to know what it's cataloging."
            ),
            confidence=Confidence.SERVER,
        )
    return CheckResult(
        check_id="resource_present",
        status=Status.PASS,
        detail=f"`resource` present: {resource_url}",
        confidence=Confidence.SERVER,
    )


def check_bazaar_extension(
    challenge: dict[str, Any], accepts: list[dict[str, Any]]
) -> CheckResult:
    """Check 3: is `extensions.bazaar` present and structurally valid?

    Schema per docs.x402.org/extensions/bazaar: `info.input.type` is
    required ("http" or "mcp"); `info.output.type` is required whenever an
    `output` block is present. Everything else under `input`/`output` is
    optional and type-dependent.
    """
    extensions = _extensions_value(challenge, accepts)
    if not isinstance(extensions, dict) or "bazaar" not in extensions:
        return CheckResult(
            check_id="bazaar_extension",
            status=Status.FAIL,
            detail="`extensions.bazaar` is missing from the payment requirement",
            fix=(
                "Add `extensions.bazaar.info.input.type` (\"http\" or \"mcp\") at "
                "minimum -- without it Bazaar has nothing to index, even if "
                "settlements succeed."
            ),
            confidence=Confidence.SERVER,
        )

    bazaar = extensions.get("bazaar")
    if not isinstance(bazaar, dict):
        return CheckResult(
            check_id="bazaar_extension",
            status=Status.FAIL,
            detail="`extensions.bazaar` is present but is not an object",
            confidence=Confidence.SERVER,
        )

    info = bazaar.get("info")
    if not isinstance(info, dict):
        return CheckResult(
            check_id="bazaar_extension",
            status=Status.FAIL,
            detail="`extensions.bazaar.info` is missing",
            fix="Add an `info` object under `extensions.bazaar` with at least `input.type`.",
            confidence=Confidence.SERVER,
        )

    input_ = info.get("input")
    input_type = input_.get("type") if isinstance(input_, dict) else None
    if input_type not in ("http", "mcp"):
        return CheckResult(
            check_id="bazaar_extension",
            status=Status.FAIL,
            detail=(
                f"`extensions.bazaar.info.input.type` is "
                f"{'missing' if input_type is None else f'invalid ({input_type!r})'}; "
                "must be \"http\" or \"mcp\""
            ),
            fix='Set `extensions.bazaar.info.input.type` to "http" or "mcp".',
            confidence=Confidence.SERVER,
        )

    output = info.get("output")
    if output is not None:
        if not isinstance(output, dict) or "type" not in output:
            return CheckResult(
                check_id="bazaar_extension",
                status=Status.WARN,
                detail=(
                    "`extensions.bazaar.info.output` is present but missing its "
                    "required `type` field"
                ),
                fix="Add `type` under `extensions.bazaar.info.output`, or omit `output` entirely.",
                confidence=Confidence.SERVER,
            )

    return CheckResult(
        check_id="bazaar_extension",
        status=Status.PASS,
        detail=f"`extensions.bazaar` is well-formed (input.type={input_type!r})",
        confidence=Confidence.SERVER,
    )


def check_description_length(
    challenge: dict[str, Any], accepts: list[dict[str, Any]], max_length: int = 500
) -> CheckResult:
    """Check 4: description over ~500 chars breaks the challenge silently."""
    resource = _resource_value(challenge, accepts)
    description = _description_value(challenge, resource, accepts)
    if not isinstance(description, str):
        return CheckResult(
            check_id="description_length",
            status=Status.SKIP,
            detail="No `description` field to check",
        )

    length = len(description)
    if length > max_length:
        return CheckResult(
            check_id="description_length",
            status=Status.FAIL,
            detail=f"`description` is {length} characters, over the ~{max_length} char limit",
            fix=(
                f"Trim `description` to under {max_length} characters. This failure "
                "mode is silent -- the challenge doesn't error, it just doesn't get "
                "indexed -- which is exactly why it's easy to miss."
            ),
            confidence=Confidence.SERVER,
        )

    return CheckResult(
        check_id="description_length",
        status=Status.PASS,
        detail=f"`description` is {length} characters (limit ~{max_length})",
        confidence=Confidence.SERVER,
    )


def check_route_template(
    challenge: dict[str, Any], accepts: list[dict[str, Any]]
) -> CheckResult:
    """Check 5 (opportunistic): `extensions.bazaar.routeTemplate`.

    Not part of any documented spec this module could confirm, but present
    in a real, live payload (routeTemplate: "/sentiment/:symbol") -- some
    implementations populate it, and when they do it directly answers the
    original checklist's wildcard-vs-named-params question instead of
    needing a manually supplied route pattern. Skips cleanly when absent
    rather than treating it as a failure, since its absence just means
    "this seller's library doesn't report it," not "this route is broken."
    """
    extensions = _extensions_value(challenge, accepts)
    route_template = None
    if isinstance(extensions, dict) and isinstance(extensions.get("bazaar"), dict):
        route_template = extensions["bazaar"].get("routeTemplate")

    if not isinstance(route_template, str) or not route_template:
        return CheckResult(
            check_id="route_template",
            status=Status.SKIP,
            detail=(
                "No `extensions.bazaar.routeTemplate` to check (not part of "
                "the base spec; some implementations populate it)"
            ),
        )

    # A bare "*" path segment (not part of a named token like ":symbol" or
    # "{symbol}") is the wildcard-routing failure mode from the original
    # checklist.
    if re.search(r"(^|/)\*(/|$)", route_template):
        return CheckResult(
            check_id="route_template",
            status=Status.FAIL,
            detail=f"`routeTemplate` ({route_template!r}) uses a bare wildcard path segment",
            fix=(
                "Switch to named-parameter route syntax (e.g. `/sentiment/:symbol` "
                "or `/sentiment/{symbol}`) instead of a wildcard `*` segment -- "
                "Bazaar's discovery metadata is more useful with named params."
            ),
            confidence=Confidence.SERVER,
        )

    return CheckResult(
        check_id="route_template",
        status=Status.PASS,
        detail=f"`routeTemplate` ({route_template!r}) uses named-parameter style",
        confidence=Confidence.SERVER,
    )


# --------------------------------------------------------------------------
# Running the full dry-check rule chain
# --------------------------------------------------------------------------


def run_checks(challenge: dict[str, Any], final_url: str) -> list[CheckResult]:
    """Run checks 1-5 against the challenge. `resource`, `description`, and
    `extensions` are read at the challenge level (the confirmed real
    shape), with `accepts[]` used only as a fallback for schema variance
    and to note when multiple payment options are offered."""
    accepts = extract_accepts(challenge)

    results = [
        check_resource_present(challenge, accepts),
        check_scheme_mismatch(challenge, accepts, final_url),
        check_bazaar_extension(challenge, accepts),
        check_description_length(challenge, accepts),
        check_route_template(challenge, accepts),
    ]

    if len(accepts) > 1:
        results.append(
            CheckResult(
                check_id="multiple_accepts_noted",
                status=Status.SKIP,
                detail=(
                    f"Challenge offers {len(accepts)} payment options; only the "
                    "first was diagnosed in this v1 pass"
                ),
            )
        )

    return results


def summarize(checks: list[CheckResult]) -> str:
    failures = [c for c in checks if c.status == Status.FAIL]
    warnings = [c for c in checks if c.status == Status.WARN]

    if not failures and not warnings:
        return (
            "No issues found in checks 1-4 (resource, scheme match, bazaar "
            "extension, description length). This doesn't guarantee a Bazaar "
            "listing -- settlement must still succeed and actually echo the "
            "extension, and the eviction/EXTENSION-RESPONSES checks aren't "
            "covered by a dry check."
        )

    parts = []
    if failures:
        parts.append(f"{len(failures)} issue(s) found: " + ", ".join(c.check_id for c in failures))
    if warnings:
        parts.append(f"{len(warnings)} warning(s): " + ", ".join(c.check_id for c in warnings))
    return "; ".join(parts)
