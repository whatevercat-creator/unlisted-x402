"""The diagnose_usage log line: one JSON record per completed diagnosis."""

import json
import logging

from fastapi.testclient import TestClient

import main
from diagnosis import CheckResult, Confidence, DiagnosisReport, Status
from test_payment import PAYER, _build_paid_client, _payment_signature_header_for

URL = "https://api.seller.test/data"


def _report(url, **kwargs):
    return DiagnosisReport(
        url=url,
        mode="dry",
        http_status=402,
        checks=[
            CheckResult(check_id="resource_present", status=Status.PASS, detail="ok"),
            CheckResult(check_id="bazaar_extension", status=Status.FAIL, detail="missing"),
            CheckResult(check_id="bazaar_index_status", status=Status.WARN, detail="lag",
                        confidence=Confidence.FACILITATOR),
        ],
        verdict="1 issue",
    )


def _usage_records(caplog):
    out = []
    for r in caplog.records:
        try:
            data = json.loads(r.getMessage())
        except ValueError:
            continue
        if data.get("event") == "diagnose_usage":
            out.append(data)
    return out


def test_usage_line_for_paid_diagnosis_includes_payer_and_result(monkeypatch, caplog):
    client, _ = _build_paid_client()

    async def fake(url, **kwargs):
        return _report(url)

    monkeypatch.setattr(main, "run_dry_check", fake)
    challenge = client.post("/diagnose", json={"url": URL}).json()
    header = _payment_signature_header_for(challenge)

    with caplog.at_level(logging.INFO, logger="x402_doctor"):
        resp = client.post("/diagnose", json={"url": URL}, headers={"PAYMENT-SIGNATURE": header})

    assert resp.status_code == 200
    [rec] = _usage_records(caplog)
    assert rec["domain"] == "api.seller.test"
    assert rec["method"] == "GET" and rec["mode"] == "dry"
    assert rec["paywall_active"] is True and rec["payer"] == PAYER
    assert rec["bazaar"] == "not_indexed_would_be_accepted"
    assert rec["checks"] == {"pass": 1, "fail": 1, "warn": 1}
    assert rec["target_http_status"] == 402
    assert isinstance(rec["duration_ms"], int)


def test_no_usage_line_for_unpaid_402(caplog):
    client, _ = _build_paid_client()
    with caplog.at_level(logging.INFO, logger="x402_doctor"):
        assert client.post("/diagnose", json={"url": URL}).status_code == 402
    assert _usage_records(caplog) == []


def test_usage_line_in_dev_mode_marks_unpaid(monkeypatch, caplog):
    monkeypatch.delenv("X402_DOCTOR_ENABLE_BAZAAR_LOOKUP", raising=False)
    app = main.create_app()

    async def fake(url, **kwargs):
        return _report(url)

    monkeypatch.setattr(main, "run_dry_check", fake)
    with caplog.at_level(logging.INFO, logger="x402_doctor"):
        TestClient(app).post("/diagnose", json={"url": URL, "method": "POST"})
    [rec] = _usage_records(caplog)
    assert rec["paywall_active"] is False and rec["payer"] is None and rec["method"] == "POST"


def test_usage_line_drops_query_string(monkeypatch, caplog):
    monkeypatch.delenv("X402_DOCTOR_ENABLE_BAZAAR_LOOKUP", raising=False)
    app = main.create_app()

    async def fake(url, **kwargs):
        return _report(url)

    monkeypatch.setattr(main, "run_dry_check", fake)
    with caplog.at_level(logging.INFO, logger="x402_doctor"):
        TestClient(app).post("/diagnose", json={"url": URL + "?api_key=secret123&x=1"})
    [rec] = _usage_records(caplog)
    assert rec["url"] == URL
    assert "secret123" not in json.dumps(rec)
