"""
Tests for the Unlisted rename: the root landing page, the top-level
`bazaar` answer on /diagnose responses, and bazaar.summarize_index_status.
"""

import pytest
from fastapi.testclient import TestClient

import bazaar
import main
import payment
from diagnosis import CheckResult, Confidence, DiagnosisReport, Status


def _check(status):
    return CheckResult(
        check_id=bazaar.CHECK_ID, status=status, detail=f"detail-{status.value}",
        confidence=Confidence.FACILITATOR,
    )


@pytest.mark.parametrize(
    "status, expected_status, expected_indexed",
    [
        (Status.PASS, "indexed", True),
        (Status.WARN, "not_indexed_would_be_accepted", False),
        (Status.FAIL, "not_indexed", False),
        (Status.SKIP, "unknown", None),
    ],
)
def test_summarize_index_status_maps_each_status(status, expected_status, expected_indexed):
    other = CheckResult(check_id="scheme_mismatch", status=Status.FAIL, detail="x")
    summary = bazaar.summarize_index_status([other, _check(status)])
    assert summary == {
        "indexed": expected_indexed,
        "status": expected_status,
        "curated": None,
        "detail": f"detail-{status.value}",
    }
    assert list(summary) == ["indexed", "status", "curated", "detail"]


def test_summarize_index_status_when_lookup_not_run():
    summary = bazaar.summarize_index_status([])
    assert summary["indexed"] is None
    assert summary["status"] == "unknown"


def test_diagnose_response_leads_with_bazaar_answer(monkeypatch):
    async def fake(url, **kwargs):
        return DiagnosisReport(
            url=url, mode="dry", http_status=402, checks=[_check(Status.PASS)], verdict="ok"
        )

    monkeypatch.delenv("X402_DOCTOR_ENABLE_BAZAAR_LOOKUP", raising=False)
    app = main.create_app()
    monkeypatch.setattr(main, "run_dry_check", fake, raising=False)
    resp = TestClient(app).post("/diagnose", json={"url": "https://api.seller.test/data"})

    assert resp.status_code == 200
    body = resp.json()
    assert list(body)[0] == "bazaar"
    assert body["bazaar"]["indexed"] is True
    # Existing report fields are unchanged.
    assert body["verdict"] == "ok" and body["mode"] == "dry" and len(body["checks"]) == 1


def test_root_page_is_unlisted_and_free():
    app = main.create_app()
    resp = TestClient(app).get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "unlisted" in resp.text.lower()
    assert "x402 Doctor" not in resp.text
    assert payment.DEFAULT_PRICE_USD in resp.text
    assert payment.DEFAULT_PAID_PRICE_USD in resp.text


def test_root_page_not_in_openapi_and_title_renamed():
    schema = TestClient(main.create_app()).get("/openapi.json").json()
    assert schema["info"]["title"] == "Unlisted"
    assert "/" not in schema["paths"]


def test_openapi_has_contact_email():
    schema = TestClient(main.create_app()).get("/openapi.json").json()
    assert schema["info"]["contact"] == {"email": "hi@unlisted.sh"}


def test_own_listing_uses_new_name():
    assert payment.SERVICE_NAME == "Unlisted"
    assert len(payment.SERVICE_NAME) <= 32


def test_root_page_stays_free_with_paywall_active():
    from test_payment import _build_paid_client

    client, _ = _build_paid_client()
    assert client.get("/").status_code == 200
    assert client.post("/diagnose", json={"url": "https://api.seller.test/x"}).status_code == 402


def test_402_body_mirrors_payment_required_header():
    import base64, json
    from test_payment import _build_paid_client

    client, _ = _build_paid_client()
    resp = client.post("/diagnose", json={"url": "https://api.seller.test/x"})
    assert resp.status_code == 402
    decoded = json.loads(base64.b64decode(resp.headers["payment-required"]))
    assert resp.json() == decoded
    assert resp.json()["accepts"], "body must carry accepts[]"
    assert "www-authenticate" not in resp.headers


def test_paid_mode_402_body_carries_paid_price():
    from test_payment import _build_paid_client

    client, _ = _build_paid_client()
    dry = client.post("/diagnose", json={"url": "https://api.seller.test/x"}).json()
    paid = client.post("/diagnose?mode=paid", json={"url": "https://api.seller.test/x"}).json()
    assert int(paid["accepts"][0]["amount"]) > int(dry["accepts"][0]["amount"])


def test_own_route_declares_bazaar_extension_for_json_body():
    from test_payment import _build_paid_client

    client, _ = _build_paid_client()
    challenge = client.post("/diagnose", json={"url": "https://api.seller.test/x"}).json()
    bazaar_ext = challenge["extensions"]["bazaar"]
    info_input = bazaar_ext["info"]["input"]
    assert info_input["method"] == "POST"
    assert info_input.get("bodyType") == "json"
    assert "url" in str(bazaar_ext["schema"])
