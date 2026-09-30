"""
POST targets: sellers whose paid route is POST with a JSON body (like
Unlisted's own /diagnose). Before this, every leg was hard-coded to GET --
the probe, the real payment replay and CDP's Bazaar lookup -- so a POST
seller got a 405 "not an x402 seller" report and a wrong index status.
"""

import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http.types import PaymentOption, RouteConfig
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.server import x402ResourceServer

import main
import outbound_payment
import safe_fetch as sf
from diagnosis import DiagnosisReport, Status
from paid_check import run_paid_check
from safe_fetch import ResolvedTarget, safe_fetch
from tests.test_bazaar import FakeBazaarClient, make_response
from tests.test_dry_check import _streaming_response
from tests.test_paid_check import SIGNER, FakeSellerFacilitator, make_ceiling

URL = "https://target.test/analyze"
BODY = {"text": "hello"}


@pytest.fixture(autouse=True)
def fake_resolver(monkeypatch):
    async def _fake(hostname: str, port: int) -> ResolvedTarget:
        return ResolvedTarget(hostname=hostname, port=port, ip="1.2.3.4", family=2)

    monkeypatch.setattr(sf, "resolve_and_validate", _fake)


class AnalyzeIn(BaseModel):
    text: str


def make_post_target():
    app = FastAPI()
    received = []

    @app.post("/analyze")
    async def analyze(payload: AnalyzeIn):
        received.append(payload.text)
        return {"ok": True, "text": payload.text}

    fake = FakeSellerFacilitator()
    server = x402ResourceServer(fake)
    server.register(outbound_payment.NETWORK, ExactEvmServerScheme())
    accepts = PaymentOption(
        scheme=outbound_payment.SCHEME,
        pay_to="0x" + "9" * 40,
        price="$0.01",
        network=outbound_payment.NETWORK,
        extra={"name": "USD Coin", "version": "2"},
    )
    app.add_middleware(PaymentMiddlewareASGI, routes={"POST /analyze": RouteConfig(accepts=accepts)}, server=server)
    return app, fake, received


async def test_get_probe_of_post_route_is_not_a_402():
    """Documents the old failure mode: probing a POST route with GET."""
    app, _, _ = make_post_target()
    resp = await safe_fetch(URL, method="GET", transport=httpx.ASGITransport(app=app))
    assert resp.status_code == 405


async def test_paid_check_on_post_target_probes_pays_and_looks_up_with_post():
    app, seller, received = make_post_target()
    bazaar_client = FakeBazaarClient(response=make_response(active=True))

    report = await run_paid_check(
        URL,
        signer=SIGNER,
        economic_ceiling=make_ceiling(),
        transport=httpx.ASGITransport(app=app),
        bazaar_client=bazaar_client,
        method="POST",
        json_body=BODY,
    )

    assert report.http_status == 402
    settlement = [c for c in report.checks if c.check_id == "settlement_echo"][0]
    assert settlement.status == Status.PASS, settlement.detail
    assert len(seller.settle_calls) == 1
    assert received == ["hello"], "paid replay must be a POST carrying the JSON body"
    assert bazaar_client.calls[0].method == "POST"


async def test_safe_fetch_sends_body_and_method():
    seen = {}

    def handler(request: httpx.Request):
        seen["method"] = request.method
        seen["body"] = request.content
        seen["ctype"] = request.headers.get("content-type")
        return _streaming_response(402, b"{}")

    await safe_fetch(
        URL,
        method="POST",
        content=json.dumps(BODY).encode(),
        headers={"content-type": "application/json"},
        transport=httpx.MockTransport(handler),
    )
    assert seen == {"method": "POST", "body": b'{"text": "hello"}', "ctype": "application/json"}


@pytest.mark.parametrize("status, expect_method, expect_body", [(303, "GET", b""), (307, "POST", b"x")])
async def test_safe_fetch_redirect_method_semantics(status, expect_method, expect_body):
    hops = []

    def handler(request: httpx.Request):
        hops.append((request.method, request.content))
        if len(hops) == 1:
            return _streaming_response(status, b"", headers={"location": "/next"})
        return _streaming_response(402, b"{}")

    await safe_fetch(URL, method="POST", content=b"x", transport=httpx.MockTransport(handler))
    assert hops[1] == (expect_method, expect_body)


# ---- /diagnose API surface ------------------------------------------------


def _capturing_app(monkeypatch):
    calls = []

    async def fake(url, **kwargs):
        calls.append(kwargs)
        return DiagnosisReport(url=url, mode="dry", http_status=402, checks=[], verdict="ok")

    monkeypatch.delenv("X402_DOCTOR_ENABLE_BAZAAR_LOOKUP", raising=False)
    app = main.create_app()
    monkeypatch.setattr(main, "run_dry_check", fake, raising=False)
    return TestClient(app), calls


def test_diagnose_passes_method_and_body_through(monkeypatch):
    client, calls = _capturing_app(monkeypatch)
    resp = client.post("/diagnose", json={"url": URL, "method": "POST", "body": BODY})
    assert resp.status_code == 200
    assert calls == [{"method": "POST", "json_body": BODY}]
    assert resp.json()["method"] == "POST"


def test_diagnose_get_default_keeps_old_call_shape(monkeypatch):
    client, calls = _capturing_app(monkeypatch)
    resp = client.post("/diagnose", json={"url": URL})
    assert resp.status_code == 200
    assert calls == [{}]
    assert resp.json()["method"] == "GET"


@pytest.mark.parametrize(
    "payload",
    [
        {"url": URL, "method": "GET", "body": BODY},  # body only for POST
        {"url": URL, "method": "PUT"},  # unsupported method
        {"url": URL, "method": "POST", "body": {"x": "a" * 9000}},  # too large
    ],
)
def test_diagnose_rejects_invalid_method_or_body(monkeypatch, payload):
    client, calls = _capturing_app(monkeypatch)
    assert client.post("/diagnose", json=payload).status_code == 422
    assert calls == []
