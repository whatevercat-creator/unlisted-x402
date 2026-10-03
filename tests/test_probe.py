"""
schema_external_refs (guide cause 9) and probe_response (guide cause 12):
the two causes Coinbase contributors confirmed in cdp-sdk #835 and #830.
"""

import json
from types import SimpleNamespace

import httpx
import pytest

import diagnosis
import dry_check
import safe_fetch as sf
from diagnosis import Status, check_schema_external_refs, extract_accepts, run_checks
from probe import build_probe_request, check_probe_response
from safe_fetch import FetchError, ResolvedTarget, SSRFBlocked
from tests.test_diagnosis import _good_challenge
from tests.test_dry_check import _streaming_response

TARGET = "https://target.test/analyze"


def _challenge(input_=None, schema=None, route_template=None, url=TARGET):
    challenge = _good_challenge()
    challenge["resource"]["url"] = url
    bazaar = {"info": {"input": input_ or {"type": "http", "method": "GET"}}}
    if schema is not None:
        bazaar["schema"] = schema
    if route_template:
        bazaar["routeTemplate"] = route_template
    challenge["extensions"] = {"bazaar": bazaar}
    return challenge


def _refs(challenge):
    return check_schema_external_refs(challenge, extract_accepts(challenge))


# --------------------------------------------------------------------------
# schema_external_refs
# --------------------------------------------------------------------------


def test_schema_refs_fail_with_exact_paths_in_info_and_schema():
    challenge = _challenge(
        schema={
            "$id": "https://api.target.test/schemas/route.json",
            "properties": {
                "input": {"properties": {"body": {"$ref": "https://cdn.test/in.json#/Body"}}},
                "output": {"anyOf": [{"$ref": "#/definitions/Ok"}, {"$ref": "defs.json#/Err"}]},
            },
        }
    )
    challenge["extensions"]["bazaar"]["info"]["output"] = {
        "type": "json",
        "schema": {"$ref": "https://cdn.test/out.json"},
    }
    result = _refs(challenge)
    assert result.status == Status.FAIL
    for expected in (
        "extensions.bazaar.info.output.schema.$ref = 'https://cdn.test/out.json'",
        "extensions.bazaar.schema.$id = 'https://api.target.test/schemas/route.json'",
        "extensions.bazaar.schema.properties.input.properties.body.$ref",
        "extensions.bazaar.schema.properties.output.anyOf[1].$ref = 'defs.json#/Err'",
    ):
        assert expected in result.detail
    assert "anyOf[0]" not in result.detail  # local "#/..." refs are fine
    assert result.detail.startswith("4 external")
    assert "Inline the schema" in result.fix


def test_schema_refs_pass_with_only_local_refs():
    challenge = _challenge(
        schema={"properties": {"input": {"$ref": "#/definitions/In"}}, "definitions": {"In": {}}}
    )
    assert _refs(challenge).status == Status.PASS


def test_schema_refs_skip_without_extension():
    challenge = _good_challenge()
    del challenge["extensions"]
    assert _refs(challenge).status == Status.SKIP


def test_schema_refs_error_is_a_skip_not_a_crash(monkeypatch):
    def boom(*_):
        raise RuntimeError("bad shape")

    monkeypatch.setattr(diagnosis, "_external_refs", boom)
    results = {r.check_id: r for r in run_checks(_challenge(), final_url=TARGET)}
    assert results["schema_external_refs"].status == Status.SKIP
    assert "bad shape" in results["schema_external_refs"].detail
    assert results["resource_present"].status == Status.PASS


# --------------------------------------------------------------------------
# probe_response: request building
# --------------------------------------------------------------------------


def test_probe_request_uses_declared_method_and_json_body():
    challenge = _challenge({"type": "http", "method": "POST", "bodyType": "json", "body": {"text": "hi"}})
    method, url, body, headers = build_probe_request(challenge, TARGET)
    assert (method, url, json.loads(body)) == ("POST", TARGET, {"text": "hi"})
    assert headers == {"content-type": "application/json"}


def test_probe_request_fills_path_and_query_params():
    challenge = _challenge(
        {"type": "http", "method": "GET", "pathParams": {"symbol": "BTC/USD"}, "queryParams": {"window": "1h"}},
        route_template="/sentiment/:symbol",
    )
    method, url, body, _ = build_probe_request(challenge, "https://target.test/sentiment/ETH?x=1")
    assert (method, url, body) == ("GET", "https://target.test/sentiment/BTC%2FUSD?window=1h", None)


async def _never_fetch(*_, **__):
    raise AssertionError("should not fetch")


@pytest.mark.parametrize(
    "input_",
    [
        {"type": "http", "method": "GET"},  # nothing to send
        {"type": "mcp", "toolName": "x"},
        {"type": "http", "method": "POST", "bodyType": "form-data", "body": {"a": 1}},
    ],
)
async def test_probe_skips_without_usable_example(input_):
    result = await check_probe_response(_challenge(input_), TARGET, _never_fetch)
    assert result.status == Status.SKIP
    assert "No usable example input" in result.detail


async def test_probe_skips_missing_path_param_value():
    challenge = _challenge(
        {"type": "http", "method": "GET", "pathParams": {"other": "1"}}, route_template="/data/:id"
    )
    result = await check_probe_response(challenge, TARGET, _never_fetch)
    assert result.status == Status.SKIP and "'id'" in result.detail


async def test_probe_never_sent_to_example_com():
    input_ = {"type": "http", "method": "POST", "bodyType": "json", "body": {"text": "hi"}}
    for url in ("https://example.com/x", "https://api.example.com/x"):
        result = await check_probe_response(_challenge(input_), url, _never_fetch)
        assert result.status == Status.SKIP
        assert "reserved example domain" in result.detail


# --------------------------------------------------------------------------
# probe_response: outcomes
# --------------------------------------------------------------------------

POST_INPUT = {"type": "http", "method": "POST", "bodyType": "json", "body": {"text": "hi"}}


def _fetch_returning(status, seen=None):
    async def fetch(url, **kwargs):
        if seen is not None:
            seen.append((url, kwargs))
        return SimpleNamespace(status_code=status)

    return fetch


async def test_probe_passes_on_402_and_sends_unpaid_example():
    seen = []
    result = await check_probe_response(_challenge(POST_INPUT), TARGET, _fetch_returning(402, seen))
    assert result.status == Status.PASS
    [(url, kwargs)] = seen
    assert url == TARGET and kwargs["method"] == "POST"
    assert json.loads(kwargs["content"]) == {"text": "hi"}
    assert not any(k.lower().startswith(("payment", "x-payment")) for k in kwargs.get("headers", {}))


@pytest.mark.parametrize("status", [400, 409, 422])
async def test_probe_fails_on_validation_status(status):
    result = await check_probe_response(_challenge(POST_INPUT), TARGET, _fetch_returning(status))
    assert result.status == Status.FAIL
    assert f"got HTTP {status}, not 402" in result.detail
    assert "validation is running before the paywall" in result.detail
    assert result.fix


@pytest.mark.parametrize("status", [200, 404, 500])
async def test_probe_fails_on_any_other_non_402(status):
    result = await check_probe_response(_challenge(POST_INPUT), TARGET, _fetch_returning(status))
    assert result.status == Status.FAIL
    assert f"got HTTP {status}, not 402" in result.detail
    assert "before the paywall" in result.detail


async def test_probe_errors_become_skips():
    async def fetch_error(*_, **__):
        raise FetchError("Timeout fetching target")

    async def ssrf(*_, **__):
        raise SSRFBlocked("All resolved addresses for target.test are blocked: ['10.0.0.1']")

    result = await check_probe_response(_challenge(POST_INPUT), TARGET, fetch_error)
    assert result.status == Status.SKIP and "Timeout fetching target" in result.detail

    result = await check_probe_response(_challenge(POST_INPUT), TARGET, ssrf)
    assert result.status == Status.SKIP and "10.0.0.1" not in result.detail


# --------------------------------------------------------------------------
# End to end through run_dry_check
# --------------------------------------------------------------------------


@pytest.fixture
def fake_resolver(monkeypatch):
    async def _fake(hostname: str, port: int) -> ResolvedTarget:
        return ResolvedTarget(hostname=hostname, port=port, ip="1.2.3.4", family=2)

    monkeypatch.setattr(sf, "resolve_and_validate", _fake)


def _patch_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)

    async def _patched(url, method="GET", **kwargs):
        return await sf.safe_fetch(url, method=method, transport=transport, **kwargs)

    monkeypatch.setattr(dry_check, "safe_fetch", _patched)


async def test_dry_check_reports_probe_rejected_by_validation(fake_resolver, monkeypatch):
    """The cdp-sdk #830 shape: an empty POST gets the 402, but the declared
    example input trips validation before the paywall and gets a 409."""
    challenge = json.dumps(_challenge(POST_INPUT)).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.content:
            return _streaming_response(409, b'{"detail": "conflict"}')
        return _streaming_response(402, challenge)

    _patch_transport(monkeypatch, handler)
    report = await dry_check.run_dry_check(TARGET, method="POST")

    checks = {c.check_id: c for c in report.checks}
    assert checks["probe_response"].status == Status.FAIL
    assert "got HTTP 409" in checks["probe_response"].detail
    assert checks["schema_external_refs"].status == Status.PASS
    assert "probe_response" in report.verdict


async def test_dry_check_survives_probe_fetch_error(fake_resolver, monkeypatch):
    challenge = json.dumps(_challenge(POST_INPUT)).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.content:
            raise httpx.ConnectError("connection reset")
        return _streaming_response(402, challenge)

    _patch_transport(monkeypatch, handler)
    report = await dry_check.run_dry_check(TARGET, method="POST")

    probe = next(c for c in report.checks if c.check_id == "probe_response")
    assert probe.status == Status.SKIP and "connection reset" in probe.detail
    assert "No issues found" in report.verdict
