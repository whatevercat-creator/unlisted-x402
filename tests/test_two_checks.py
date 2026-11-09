"""resource_url_match and standard_payment_option (added Nov 9, 2026).

Edge cases from the Phase 1 research: host/path comparison rules for
resource.url, and Circle Gateway options (scheme "exact" with
extra.name "GatewayWalletBatched", per Circle's Gateway Nanopayments docs).
Local only: no network, no payments.
"""

import base64
import json
import pathlib

import pytest
from fastapi.testclient import TestClient

import main
from diagnosis import (
    Status,
    check_resource_url_match,
    check_standard_payment_option,
    run_checks,
)
from tests.test_payment import PAY_TO, FakeFacilitatorClient

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "crypto_sentiment"

EXACT_USDC = {
    "scheme": "exact",
    "network": "eip155:8453",
    "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "amount": "10000",
    "payTo": "0x2222222222222222222222222222222222222222",
    "maxTimeoutSeconds": 300,
    "extra": {"name": "USD Coin", "version": "2"},
}
GATEWAY = {
    "scheme": "exact",
    "network": "eip155:5042002",
    "asset": "0x3333333333333333333333333333333333333333",
    "amount": "10000",
    "payTo": "0x2222222222222222222222222222222222222222",
    "maxTimeoutSeconds": 604900,
    "extra": {
        "name": "GatewayWalletBatched",
        "version": "1",
        "verifyingContract": "0x4444444444444444444444444444444444444444",
    },
}


def _url_check(resource_url, served_from):
    challenge = {"x402Version": 2, "resource": {"url": resource_url}, "accepts": [EXACT_USDC]}
    return check_resource_url_match(challenge, [EXACT_USDC], served_from)


# --- resource_url_match -------------------------------------------------------

@pytest.mark.parametrize(
    "resource_url, served_from",
    [
        ("https://api.example.com/data", "https://api.example.com/data"),
        ("https://API.Example.COM/data", "https://api.example.com/data"),
        ("https://api.example.com:443/data", "https://api.example.com/data"),
        ("https://api.example.com/data", "https://api.example.com:443/data"),
        ("http://api.example.com:80/data", "http://api.example.com/data"),
        ("https://api.example.com/data/", "https://api.example.com/data"),
        ("https://api.example.com/data", "https://api.example.com/data/"),
        ("https://api.example.com", "https://api.example.com/"),
        ("https://api.example.com/sentiment/:symbol", "https://api.example.com/sentiment/BTC"),
        ("https://api.example.com/sentiment/{symbol}", "https://api.example.com/sentiment/BTC"),
        ("https://api.example.com/a/{x}/b/:y", "https://api.example.com/a/1/b/2"),
        ("https://api.example.com/data?a=1", "https://api.example.com/data?b=2"),
        ("https://api.example.com/data#frag", "https://api.example.com/data"),
        ("https://api.example.com/caf%C3%A9/%7Eme", "https://api.example.com/café/~me"),
        ("https://api.example.com./data", "https://api.example.com/data"),
        # Scheme differences are scheme_mismatch's job, not this check's.
        ("http://api.example.com/data", "https://api.example.com/data"),
    ],
)
def test_resource_url_match_passes(resource_url, served_from):
    result = _url_check(resource_url, served_from)
    assert result.status == Status.PASS, result.detail


@pytest.mark.parametrize(
    "resource_url, served_from, what",
    [
        ("https://other.example.com/data", "https://api.example.com/data", "host"),
        ("https://example.com/data", "https://www.example.com/data", "host"),
        ("http://localhost:8000/data", "https://api.example.com/data", "host"),
        ("https://myapp.onrender.com/data", "https://api.example.com/data", "host"),
        ("https://api.example.com:8443/data", "https://api.example.com/data", "host"),
        ("https://api.example.com/other", "https://api.example.com/data", "path"),
        ("https://api.example.com/Data", "https://api.example.com/data", "path"),
        ("https://api.example.com/sentiment/:symbol", "https://api.example.com/sentiment", "path"),
        ("https://api.example.com/sentiment/:symbol", "https://api.example.com/sentiment/BTC/x", "path"),
        ("https://api.example.com/a/:x/b", "https://api.example.com/a//b", "path"),
        ("https://internal:9000/v2/data", "https://api.example.com/data", "host and path"),
    ],
)
def test_resource_url_match_warns(resource_url, served_from, what):
    result = _url_check(resource_url, served_from)
    assert result.status == Status.WARN, result.detail
    assert f"different {what} than" in result.detail
    assert "public origin" in result.fix
    assert "trust the proxy" in result.fix


def test_relative_resource_url_warns():
    # Circle's own seller quickstart uses a relative url like this.
    result = _url_check("/premium-data", "https://api.example.com/premium-data")
    assert result.status == Status.WARN
    assert "not a full URL" in result.detail


@pytest.mark.parametrize("challenge", [
    {"x402Version": 2, "accepts": [EXACT_USDC]},
    {"x402Version": 2, "resource": {"url": ""}, "accepts": [EXACT_USDC]},
    {"x402Version": 2, "resource": {"description": "no url"}, "accepts": [EXACT_USDC]},
])
def test_resource_url_match_skips_without_url(challenge):
    assert check_resource_url_match(challenge, [EXACT_USDC], "https://a.example/x").status == Status.SKIP


def test_unparseable_port_skips():
    result = _url_check("https://api.example.com:notaport/data", "https://api.example.com/data")
    assert result.status == Status.SKIP


def test_resource_url_string_form_is_read():
    challenge = {"x402Version": 2, "resource": "https://api.example.com/data", "accepts": [EXACT_USDC]}
    assert check_resource_url_match(challenge, [EXACT_USDC], "https://api.example.com/data").status == Status.PASS


def test_never_fails():
    # Nothing documented says the Bazaar drops a route for a mismatch.
    for resource_url in ("https://x.example/a", "/rel", "https://api.example.com/b"):
        assert _url_check(resource_url, "https://api.example.com/data").status != Status.FAIL


# --- standard_payment_option --------------------------------------------------

def _option_check(accepts):
    return check_standard_payment_option({"x402Version": 2, "accepts": accepts})


def test_exact_option_passes():
    assert _option_check([EXACT_USDC]).status == Status.PASS


def test_exact_next_to_gateway_passes():
    result = _option_check([GATEWAY, EXACT_USDC])
    assert result.status == Status.PASS
    assert "1 Circle Gateway option" in result.detail


def test_gateway_only_warns_even_though_scheme_is_exact():
    result = _option_check([GATEWAY])
    assert GATEWAY["scheme"] == "exact"
    assert result.status == Status.WARN
    assert "GatewayWalletBatched" in result.detail
    assert "plain `exact` option" in result.fix


def test_two_gateway_options_warn():
    other = {**GATEWAY, "network": "eip155:8453"}
    assert _option_check([GATEWAY, other]).status == Status.WARN


def test_gateway_plus_non_exact_scheme_warns():
    upto = {**EXACT_USDC, "scheme": "upto"}
    assert _option_check([GATEWAY, upto]).status == Status.WARN


def test_gateway_name_match_is_exact():
    lookalike = {**EXACT_USDC, "extra": {"name": "gatewaywalletbatched", "version": "1"}}
    assert _option_check([lookalike]).status == Status.PASS


def test_exact_without_extra_passes():
    plain = {k: v for k, v in EXACT_USDC.items() if k != "extra"}
    assert _option_check([plain]).status == Status.PASS


def test_neither_exact_nor_gateway_skips():
    assert _option_check([{**EXACT_USDC, "scheme": "upto"}]).status == Status.SKIP


@pytest.mark.parametrize("challenge", [
    {"x402Version": 2},
    {"x402Version": 2, "accepts": []},
    {"x402Version": 2, "accepts": "nope"},
    {"x402Version": 2, "accepts": ["nope", 3]},
])
def test_no_accepts_skips(challenge):
    assert check_standard_payment_option(challenge).status == Status.SKIP


def test_both_checks_run_in_the_dry_chain():
    ids = [c.check_id for c in run_checks({"x402Version": 2, "accepts": [GATEWAY]}, "https://a.example/x")]
    assert "resource_url_match" in ids
    assert "standard_payment_option" in ids


# --- real 402s ----------------------------------------------------------------

def _by_id(checks):
    return {c.check_id: c for c in checks}


def test_unlisted_own_402_passes_both():
    client = TestClient(main.create_app(facilitator_client=FakeFacilitatorClient(), pay_to=PAY_TO))
    resp = client.post("/diagnose", json={"url": "https://seller.test/x"})
    assert resp.status_code == 402
    challenge = json.loads(base64.b64decode(resp.headers["payment-required"]))
    checks = _by_id(run_checks(challenge, final_url=str(resp.url)))
    assert checks["resource_url_match"].status == Status.PASS, checks["resource_url_match"].detail
    assert checks["standard_payment_option"].status == Status.PASS


@pytest.mark.parametrize("name, served_from", [
    ("sentiment_BTC", "https://crypto-sentiment-x402.onrender.com/sentiment/BTC"),
    ("history_BTC", "https://crypto-sentiment-x402.onrender.com/history/BTC"),
    ("movers", "https://crypto-sentiment-x402.onrender.com/movers"),
])
def test_crypto_sentiment_saved_402s_pass_both(name, served_from):
    # Decoded PAYMENT-REQUIRED headers from crypto-sentiment's live 402s,
    # captured 2026-11-09 (its .baseline402/live-before).
    challenge = json.loads((FIXTURES / f"{name}.json").read_text())
    checks = _by_id(run_checks(challenge, final_url=served_from))
    assert checks["resource_url_match"].status == Status.PASS, checks["resource_url_match"].detail
    assert checks["standard_payment_option"].status == Status.PASS
