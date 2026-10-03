"""
probe_response: reproduce the probe CDP's Bazaar sends after a settlement.

CDP probes a route with the example input from its `extensions.bazaar.info.
input` declaration and expects a 402. A route whose input validation runs
before the x402 middleware answers that probe with a 400/409/422 instead,
and indexing stops there (cdp-sdk #830: the probe got a 409). This check
builds the same request from the declaration -- method, path params
(through `routeTemplate`), query params and example body -- and sends it
unpaid through safe_fetch.

The probe goes to the URL that actually served the challenge (same host
the caller submitted, so the same rate-limit accounting applies), with its
path rebuilt from `routeTemplate` + `pathParams` when both are declared and
its query replaced by `queryParams` when declared.

Never raises: any error, including SSRFBlocked/FetchError from the probe
itself, becomes a SKIP with the reason, so the rest of the report stands.
"""

from __future__ import annotations

import json
import re
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import quote, urlencode, urlparse, urlunparse

from diagnosis import CheckResult, Confidence, Status, _extensions_value, extract_accepts
from safe_fetch import SSRFBlocked

# RFC 2606 reserved example domains: never probed (and rejected as /diagnose
# targets in main.py).
EXAMPLE_DOMAINS = ("example.com", "example.org", "example.net")

# Statuses that mean input validation answered before the paywall.
VALIDATION_STATUSES = (400, 409, 422)

_METHODS = ("GET", "HEAD", "DELETE", "POST", "PUT", "PATCH")
_BODY_METHODS = ("POST", "PUT", "PATCH")

Fetch = Callable[..., Awaitable[Any]]


def is_example_domain(url: str) -> bool:
    host = (urlparse(url).hostname or "").rstrip(".").lower()
    return any(host == d or host.endswith("." + d) for d in EXAMPLE_DOMAINS)


class _NoExample(Exception):
    """The declaration has no example input this check can send."""


def _fill_template(template: str, path_params: dict[str, Any]) -> str:
    """Substitute `:name`, `{name}` and `[name]` segments of `template`."""

    def sub(match: re.Match) -> str:
        name = match.group(1) or match.group(2) or match.group(3)
        if name not in path_params:
            raise _NoExample(f"no example value for path parameter {name!r}")
        return quote(str(path_params[name]), safe="")

    return re.sub(r":(\w+)|\{(\w+)\}|\[(\w+)\]", sub, template)


def build_probe_request(
    challenge: dict[str, Any], served_url: str, default_method: str = "GET"
) -> tuple[str, str, Optional[bytes], dict[str, str]]:
    """(method, url, body, headers) for CDP's probe. Raises _NoExample when
    there's nothing usable in the declaration."""
    extensions = _extensions_value(challenge, extract_accepts(challenge))
    bazaar = extensions.get("bazaar") if isinstance(extensions, dict) else None
    info = bazaar.get("info") if isinstance(bazaar, dict) else None
    input_ = info.get("input") if isinstance(info, dict) else None
    if not isinstance(input_, dict):
        raise _NoExample("no `extensions.bazaar.info.input` declared")
    if input_.get("type") != "http":
        raise _NoExample(f"input type is {input_.get('type')!r}, not \"http\"")

    method = str(input_.get("method") or default_method).upper()
    if method not in _METHODS:
        raise _NoExample(f"unsupported declared method {method!r}")

    parsed = urlparse(served_url)
    path, query = parsed.path, parsed.query
    used_example = False

    path_params = input_.get("pathParams")
    template = bazaar.get("routeTemplate")
    if isinstance(path_params, dict) and path_params and isinstance(template, str) and template:
        path = _fill_template(template, path_params)
        used_example = True

    query_params = input_.get("queryParams")
    if isinstance(query_params, dict) and query_params:
        query = urlencode(
            {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in query_params.items()},
            doseq=True,
        )
        used_example = True

    body: Optional[bytes] = None
    headers: dict[str, str] = {}
    if "body" in input_ and input_["body"] is not None and method in _BODY_METHODS:
        body_type = input_.get("bodyType", "json")
        example = input_["body"]
        if body_type == "json":
            body = json.dumps(example).encode()
            headers["content-type"] = "application/json"
        elif body_type == "text" and isinstance(example, str):
            body = example.encode()
            headers["content-type"] = "text/plain"
        else:
            raise _NoExample(f"can't send a {body_type!r} example body")
        used_example = True

    if not used_example:
        raise _NoExample("the declaration has no example body, query or path params")

    url = urlunparse(parsed._replace(path=path, query=query, fragment=""))
    return method, url, body, headers


def _skip(detail: str) -> CheckResult:
    return CheckResult(check_id="probe_response", status=Status.SKIP, detail=detail)


async def check_probe_response(
    challenge: dict[str, Any], served_url: str, fetch: Fetch, default_method: str = "GET"
) -> CheckResult:
    """Send CDP's probe (built by build_probe_request) through `fetch`
    (safe_fetch, or a caller's wrapper of it) and expect a 402."""
    try:
        try:
            method, url, body, headers = build_probe_request(challenge, served_url, default_method)
        except _NoExample as e:
            return _skip(f"No usable example input to probe with: {e}")

        if is_example_domain(url):
            return _skip(f"Not probing {urlparse(url).hostname}: it's a reserved example domain")

        kwargs: dict[str, Any] = {"method": method}
        if body is not None:
            kwargs["content"] = body
        if headers:
            kwargs["headers"] = headers
        response = await fetch(url, **kwargs)
        status = response.status_code
        sent = f"{method} {url}" + (" with the declared example body" if body is not None else "")

        if status == 402:
            return CheckResult(
                check_id="probe_response",
                status=Status.PASS,
                detail=f"CDP-style probe ({sent}) got HTTP 402",
                confidence=Confidence.SERVER,
            )

        why = (
            "validation is running before the paywall"
            if status in VALIDATION_STATUSES
            else "something, most likely input validation, is running before the paywall"
        )
        return CheckResult(
            check_id="probe_response",
            status=Status.FAIL,
            detail=(
                f"CDP-style probe ({sent}) got HTTP {status}, not 402: {why}. "
                "CDP's Bazaar sends this same request after a settlement and stops "
                "indexing when it isn't answered with a 402."
            ),
            fix=(
                "Make the example input in `extensions.bazaar.info.input` pass your "
                "route's own validation, or let the x402 middleware answer before "
                "validation runs, so an unpaid request with that input gets a 402. "
                "Then make a new settlement through CDP."
            ),
            confidence=Confidence.SERVER,
        )
    except SSRFBlocked:
        # Vague on purpose, like main.py's 400: no network-mapping oracle.
        return _skip("Probe could not be sent: the probe URL can't be checked")
    except Exception as e:  # noqa: BLE001 -- never fail the whole diagnosis
        return _skip(f"Probe could not be sent: {type(e).__name__}: {e}")
