"""The "Want it done for you?" next_step line on /diagnose reports.

Present only when at least one check FAILed (dry or paid mode); never on an
all-pass/skip/warn report, never in the unpaid 402, never in /guide. Purely
additive: every existing report key keeps its name and value. Local fakes
only: no network, no payments.
"""

import base64
import json
import logging
import re
from dataclasses import asdict

import pytest
from fastapi.testclient import TestClient

import main
from diagnosis import CheckResult, Confidence, DiagnosisReport, Status
from limits import SlidingWindowRateLimiter
from tests.test_payment import PAY_TO, FakeFacilitatorClient, _payment_signature_header_for

TARGET = "https://seller.test/paid-route"
BANNED = ["get listed", "guarantee", "will appear", "will be listed", "$", "usdc"]


@pytest.fixture(autouse=True)
def reset_limiters():
    main.caller_limiter = SlidingWindowRateLimiter(limit=main.CALLER_LIMIT, window_seconds=main.CALLER_WINDOW_SECONDS)
    main.domain_limiter = SlidingWindowRateLimiter(limit=main.DOMAIN_LIMIT, window_seconds=main.DOMAIN_WINDOW_SECONDS)
    main.paid_test_domain_limiter = SlidingWindowRateLimiter(
        limit=main.PAID_TEST_DOMAIN_LIMIT, window_seconds=main.PAID_TEST_DOMAIN_WINDOW_SECONDS
    )
    yield


def _check(check_id, status):
    return CheckResult(check_id=check_id, status=status, detail=f"{check_id} detail", confidence=Confidence.SERVER)


def _report(url, mode, *statuses):
    return DiagnosisReport(
        url=url,
        mode=mode,
        http_status=402,
        checks=[_check(f"c{i}", s) for i, s in enumerate(statuses)],
        verdict="the verdict, unchanged",
    )


def _dry(monkeypatch, *statuses):
    async def fake(url, **kwargs):
        return _report(url, "dry", *statuses)

    monkeypatch.setattr(main, "run_dry_check", fake)
    resp = TestClient(main.app).post("/diagnose", json={"url": TARGET})
    assert resp.status_code == 200
    return resp.json()


def _cta_lines(caplog):
    return [json.loads(r.message) for r in caplog.records if "fix_cta_shown" in r.message]


# --------------------------------------------------------------------------
# The line itself
# --------------------------------------------------------------------------


def test_cta_text_links_to_fix_and_has_no_banned_wording_or_price():
    text = main.FIX_CTA.lower()
    assert "https://unlisted.sh/fix" in text
    for phrase in BANNED:
        assert phrase not in text, phrase
    assert not re.search(r"\d+\.\d\d", text), "no hard-coded price"
    # Listing Fix is a written report the seller applies; don't say we apply it.
    assert "we apply" not in text


# --------------------------------------------------------------------------
# Dry mode
# --------------------------------------------------------------------------


def test_dry_report_with_a_fail_has_next_step(monkeypatch):
    body = _dry(monkeypatch, Status.PASS, Status.FAIL)
    assert body["next_step"] == main.FIX_CTA


@pytest.mark.parametrize(
    "statuses",
    [
        (Status.PASS, Status.PASS),
        (Status.SKIP, Status.SKIP),
        (Status.PASS, Status.WARN, Status.INFO, Status.SKIP),
        (),
    ],
    ids=["all-pass", "all-skip", "pass-warn-info-skip", "no-checks"],
)
def test_dry_report_without_a_fail_has_no_next_step(monkeypatch, statuses):
    body = _dry(monkeypatch, *statuses)
    assert "next_step" not in body
    assert main.FIX_CTA not in json.dumps(body)


def test_existing_report_keys_and_values_unchanged(monkeypatch):
    failing = _dry(monkeypatch, Status.FAIL, Status.PASS)
    # Same report shape the handler built before next_step existed.
    expected = asdict(_report(TARGET, "dry", Status.FAIL, Status.PASS))
    expected.pop("curated")
    expected.pop("unpaid_reason")
    without = {k: v for k, v in failing.items() if k != "next_step"}
    assert set(failing) - set(without) == {"next_step"}
    for key, value in json.loads(json.dumps(expected, default=lambda o: o.value)).items():
        assert without[key] == value, key
    assert without["method"] == "GET"
    assert "bazaar" in without
    # And the key order is unchanged, with next_step last.
    assert list(failing)[-1] == "next_step"


def test_cta_logged_once_without_domain_or_payer(monkeypatch, caplog):
    with caplog.at_level(logging.INFO, logger="x402_doctor"):
        _dry(monkeypatch, Status.FAIL, Status.FAIL)
    lines = _cta_lines(caplog)
    assert len(lines) == 1
    assert lines[0]["event"] == "fix_cta_shown"
    assert set(lines[0]) <= {"event", "referrer", "kind"}
    assert "seller.test" not in json.dumps(lines[0])


def test_cta_not_logged_without_a_fail(monkeypatch, caplog):
    with caplog.at_level(logging.INFO, logger="x402_doctor"):
        _dry(monkeypatch, Status.PASS, Status.WARN)
    assert _cta_lines(caplog) == []


# --------------------------------------------------------------------------
# Paid mode, the 402 and /guide
# --------------------------------------------------------------------------


def _paid_client(monkeypatch, *statuses):
    async def fake_paid(url, **kwargs):
        return _report(url, "paid", *statuses)

    monkeypatch.setattr(main, "run_paid_check", fake_paid)
    app = main.create_app(facilitator_client=FakeFacilitatorClient(), pay_to=PAY_TO, outbound_signer=object())
    return TestClient(app)


def _pay_and_post(client):
    unpaid = client.post("/diagnose?mode=paid", json={"url": TARGET})
    assert unpaid.status_code == 402
    challenge = json.loads(base64.b64decode(unpaid.headers["payment-required"]))
    header = _payment_signature_header_for(challenge)
    return client.post("/diagnose?mode=paid", json={"url": TARGET}, headers={"PAYMENT-SIGNATURE": header})


def test_paid_report_with_a_fail_has_next_step(monkeypatch):
    resp = _pay_and_post(_paid_client(monkeypatch, Status.PASS, Status.FAIL))
    assert resp.status_code == 200
    assert resp.json()["mode"] == "paid"
    assert resp.json()["next_step"] == main.FIX_CTA


def test_paid_report_all_pass_has_no_next_step(monkeypatch):
    resp = _pay_and_post(_paid_client(monkeypatch, Status.PASS, Status.SKIP))
    assert resp.status_code == 200
    assert "next_step" not in resp.json()


@pytest.mark.parametrize("path", ["/diagnose", "/diagnose?mode=paid"])
def test_cta_not_in_the_unpaid_402(monkeypatch, path):
    client = _paid_client(monkeypatch, Status.FAIL)
    resp = client.post(path, json={"url": TARGET})
    assert resp.status_code == 402
    decoded = base64.b64decode(resp.headers["payment-required"]).decode()
    for blob in (decoded, resp.text):
        assert "Listing Fix" not in blob
        assert "unlisted.sh/fix" not in blob
        assert "Want it done for you" not in blob


def test_cta_not_in_the_free_guide():
    resp = TestClient(main.app).get("/guide")
    assert resp.status_code == 200
    assert main.FIX_CTA not in resp.text
    assert "next_step" not in resp.text
