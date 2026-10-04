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
    # CDP discovery's curated flag (bazaar.lookup_curated); None = unknown.
    # main.py moves it into the top-level `bazaar` summary.
    curated: Optional[bool] = None
    # Paid mode only: why no test payment was made although the target
    # answered with a 402. main.py answers with just this reason, uncharged.
    unpaid_reason: Optional[str] = None

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
    shape; fall back to the first accepts[] entry otherwise, so a misplaced
    Bazaar block still gets its contents checked (extensions_placement
    reports the placement itself)."""
    if _bazaar_location(challenge, accepts) == "accepts[0]":
        return accepts[0]["extensions"]
    extensions = challenge.get("extensions")
    if isinstance(extensions, dict):
        return extensions
    if accepts and isinstance(accepts[0].get("extensions"), dict):
        return accepts[0]["extensions"]
    return None


def _bazaar_location(challenge: dict[str, Any], accepts: list[dict[str, Any]]) -> Optional[str]:
    """Where `extensions.bazaar` was found: "top" (next to `accepts`),
    "accepts[0]", or None. Top level wins when both have one."""
    top = challenge.get("extensions")
    if isinstance(top, dict) and "bazaar" in top:
        return "top"
    if accepts and accepts[0] is not challenge:
        nested = accepts[0].get("extensions")
        if isinstance(nested, dict) and "bazaar" in nested:
            return "accepts[0]"
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

    where = (
        " but sits inside accepts[0] (see extensions_placement)"
        if _bazaar_location(challenge, accepts) == "accepts[0]"
        else ""
    )
    return CheckResult(
        check_id="bazaar_extension",
        status=Status.PASS,
        detail=f"`extensions.bazaar` is well-formed (input.type={input_type!r}){where}",
        confidence=Confidence.SERVER,
    )


def check_extensions_placement(
    challenge: dict[str, Any], accepts: list[dict[str, Any]]
) -> CheckResult:
    """Is `extensions` at the top level of the 402 body, next to `accepts`?

    The x402 v2 spec (x402-specification-v2.md section 5.1) defines
    `extensions` on PaymentRequired only; a PaymentRequirements entry in
    `accepts` has no such field. Clients echo PaymentRequired.extensions
    into the PaymentPayload, and the SDK's PaymentRequirements model drops
    an `extensions` key when it parses an accepts[] entry, so a block placed
    there never reaches CDP's facilitator. bazaar_extension still checks the
    nested block's contents.
    """
    location = _bazaar_location(challenge, accepts)
    if location == "top":
        return CheckResult(
            check_id="extensions_placement",
            status=Status.PASS,
            detail="`extensions` is at the top level of the 402 body, next to `accepts`",
            confidence=Confidence.SERVER,
        )
    if location is None:
        return CheckResult(
            check_id="extensions_placement",
            status=Status.SKIP,
            detail="No `extensions.bazaar` block found (see bazaar_extension check)",
        )
    return CheckResult(
        check_id="extensions_placement",
        status=Status.FAIL,
        detail=(
            "`extensions.bazaar` is inside accepts[0], not at the top level of the "
            "402 body. The x402 v2 spec defines `extensions` only next to `accepts`; "
            "x402 clients copy it from there into the payment they send, and the x402 "
            "SDK drops an `extensions` key inside an accepts[] entry, so a payment "
            "made with an x402 client carries no Bazaar declaration to CDP's facilitator."
        ),
        fix=(
            "Move `extensions` out of accepts[0] to the top level of the 402 body, "
            "next to `accepts` (and in the decoded PAYMENT-REQUIRED header). Then make "
            "a new settlement through CDP."
        ),
        confidence=Confidence.SERVER,
    )


def check_description_length(
    challenge: dict[str, Any], accepts: list[dict[str, Any]], max_length: int = 500
) -> CheckResult:
    """Check 4: CDP's facilitator rejects verify/settle when the description is over 500 characters."""
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
            detail=f"`description` is {length} characters, over the {max_length} character limit",
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
        detail=f"`description` is {length} characters (limit {max_length})",
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


def _json_path(parent: str, key: Any) -> str:
    if isinstance(key, int):
        return f"{parent}[{key}]"
    if re.fullmatch(r"[A-Za-z_$][\w$]*", key):
        return f"{parent}.{key}"
    return f"{parent}[{json.dumps(key)}]"


def _external_refs(node: Any, path: str) -> list[tuple[str, str]]:
    """(json path, value) for every `$ref`/`$id` string under `node` that
    isn't local to the document (i.e. doesn't start with "#")."""
    found: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            child = _json_path(path, key)
            if key in ("$ref", "$id") and isinstance(value, str) and not value.startswith("#"):
                found.append((child, value))
            else:
                found.extend(_external_refs(value, child))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            found.extend(_external_refs(value, _json_path(path, i)))
    return found


def check_schema_external_refs(
    challenge: dict[str, Any], accepts: list[dict[str, Any]]
) -> CheckResult:
    """Check 6: external `$ref`/`$id` anywhere in the Bazaar declaration
    (`info` and `schema`, input and output). CDP's indexer rejects them with
    "schema must not contain external $ref/$id references" (cdp-sdk #835),
    even when /v2/x402/validate passes the route (x402 #3045)."""
    extensions = _extensions_value(challenge, accepts)
    bazaar = extensions.get("bazaar") if isinstance(extensions, dict) else None
    if not isinstance(bazaar, dict):
        return CheckResult(
            check_id="schema_external_refs",
            status=Status.SKIP,
            detail="No `extensions.bazaar` declaration to scan (see bazaar_extension check)",
        )

    found: list[tuple[str, str]] = []
    for part in ("info", "schema"):
        found.extend(_external_refs(bazaar.get(part), f"extensions.bazaar.{part}"))

    if found:
        listed = "; ".join(f"{path} = {value!r}" for path, value in found)
        return CheckResult(
            check_id="schema_external_refs",
            status=Status.FAIL,
            detail=(
                f"{len(found)} external $ref/$id reference(s) in the Bazaar "
                f"declaration: {listed}"
            ),
            fix=(
                "Inline the schema: replace each external `$ref` with the "
                "definition it points to, and drop external `$id` values. Only "
                "local references (\"#/...\") are allowed. CDP's indexer rejects "
                "the route otherwise, even though settlement and /v2/x402/validate "
                "succeed. Then make a new settlement through CDP."
            ),
            confidence=Confidence.SERVER,
        )

    return CheckResult(
        check_id="schema_external_refs",
        status=Status.PASS,
        detail="No external $ref/$id references in the Bazaar declaration",
        confidence=Confidence.SERVER,
    )


def _guarded(check_id: str, check, *args) -> CheckResult:
    """Run a check that must never take the whole diagnosis down with it."""
    try:
        return check(*args)
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            check_id=check_id,
            status=Status.SKIP,
            detail=f"Check could not run: {type(e).__name__}: {e}",
        )


# --------------------------------------------------------------------------
# Running the full dry-check rule chain
# --------------------------------------------------------------------------


def run_checks(challenge: dict[str, Any], final_url: str) -> list[CheckResult]:
    """Run checks 1-6 against the challenge. `resource`, `description`, and
    `extensions` are read at the challenge level (the confirmed real
    shape), with `accepts[]` used only as a fallback for schema variance
    and to note when multiple payment options are offered."""
    accepts = extract_accepts(challenge)

    results = [
        check_resource_present(challenge, accepts),
        check_scheme_mismatch(challenge, accepts, final_url),
        check_bazaar_extension(challenge, accepts),
        check_extensions_placement(challenge, accepts),
        check_description_length(challenge, accepts),
        check_route_template(challenge, accepts),
        _guarded("schema_external_refs", check_schema_external_refs, challenge, accepts),
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


# Statuses that mean input validation answered before the paywall.
VALIDATION_STATUSES = (400, 409, 422)


def non_402_verdict(status: int, *, method: str = "GET", mode: str = "dry", allow: Optional[str] = None) -> str:
    """Verdict for a submitted URL that answered with something other than
    a 402, naming the most likely cause for the common statuses. `allow`
    is the target's Allow header, quoted back on a 405."""
    verdict = f"Expected HTTP 402, got {status}."
    if status in VALIDATION_STATUSES:
        verdict += (
            " Input validation is most likely running before the paywall, so "
            "crawlers, agents and CDP's probe get this error and never see the price. "
            "To check the challenge, submit the URL with its required parameters "
            "(path or query), or for a POST route set `method` to POST and send an "
            "example `body`. Then fix the route so the x402 middleware answers an "
            "unpaid request with a 402 before validation runs."
        )
    elif status in (401, 403):
        verdict += (
            " The route asks for some other authentication (an API key, a login or "
            "an IP allowlist) before the paywall, so x402 clients and CDP's probe "
            "never get a 402. Put the x402 middleware in front of that check, so an "
            "unpaid request needs nothing but the payment."
        )
    elif status == 404:
        verdict += (
            f" Nothing is served at this URL for {method}. Check the path, including "
            "any path parameters, and that the paid route is deployed."
        )
    elif status == 405:
        other = "POST" if method == "GET" else "GET"
        verdict += f" The route doesn't accept {method}"
        verdict += f" (its Allow header says: {allow})." if allow else "."
        verdict += f" Submit it again with `method` set to {other}"
        verdict += " and an example `body` if the route needs one." if other == "POST" else "."
    elif 500 <= status <= 599:
        verdict += (
            " The server errored before it could answer with a 402, so nobody can see "
            "the price or pay. Check its logs. Missing input can cause this too, so also "
            "try the URL with its required parameters."
        )
    elif status == 200:
        verdict += " This endpoint may not require payment at all, or may not be an x402 seller."
    elif mode == "dry":
        verdict += " Can't run the payment-requirement checks without a 402 challenge to inspect."

    if mode == "paid":
        verdict += " No test payment was made: there's no 402 challenge to pay against."
    return verdict


def summarize(checks: list[CheckResult]) -> str:
    """Build the report's top-level verdict sentence.

    Mode-aware: the "clean" case used to hardcode "checks 1-4" regardless of
    which checks actually ran, so a paid-mode report with a real, *passing*
    settlement_echo result (or an enabled bazaar_index_status result) read
    identically to a dry-mode report that never attempted either -- a real
    settlement and a real Bazaar-catalog confirmation are both much stronger
    signals than "the schema looks fine," and deserve their own sentence
    rather than being silently folded into "no issues found." A FAIL on
    either check still routes through the ordinary failures/warnings branch
    below unchanged, since this function only special-cases the fully-clean
    case; a SKIP on either (not configured, or declined for a documented
    reason) is deliberately left unmentioned here -- that reason already
    lives on the check itself.
    """
    failures = [c for c in checks if c.status == Status.FAIL]
    warnings = [c for c in checks if c.status == Status.WARN]

    if not failures and not warnings:
        by_id = {c.check_id: c for c in checks}
        schema_check_count = len(
            [c for c in checks if c.check_id not in ("settlement_echo", "bazaar_index_status", "multiple_accepts_noted")]
        )
        sentence = (
            f"No issues found in the {schema_check_count} schema/config check(s) "
            "(resource, scheme match, bazaar extension and its placement, description length, "
            "external schema refs, and route template and example-input probe "
            "when reported)."
        )

        settlement = by_id.get("settlement_echo")
        if settlement is not None and settlement.status == Status.PASS:
            sentence += " A real test payment was also attempted and settled successfully."
        elif settlement is None:
            # Dry mode never attempts a real payment -- the schema looking
            # right is not the same guarantee a real settlement would be.
            sentence += (
                " This doesn't guarantee a Bazaar listing -- settlement must "
                "still succeed and actually echo the extension, and the "
                "eviction/EXTENSION-RESPONSES checks aren't covered by a dry check."
            )

        bazaar = by_id.get("bazaar_index_status")
        if bazaar is not None and bazaar.status == Status.PASS:
            sentence += " Confirmed currently indexed in the CDP Bazaar catalog."

        return sentence

    parts = []
    if failures:
        parts.append(f"{len(failures)} issue(s) found: " + ", ".join(c.check_id for c in failures))
    if warnings:
        parts.append(f"{len(warnings)} warning(s): " + ", ".join(c.check_id for c in warnings))
    return "; ".join(parts)
