"""
Tests for the POST /diagnose FastAPI endpoint. `run_dry_check` is
monkeypatched directly here rather than mocking the network again -- the
fetch/parse/diagnose pipeline already has its own tests; this file is about
the HTTP layer (status codes, request validation, error mapping) on top of
it.
"""

import pytest
from fastapi.testclient import TestClient

import main
from diagnosis import CheckResult, Confidence, DiagnosisReport, Status
from limits import SlidingWindowRateLimiter
from safe_fetch import FetchError, SSRFBlocked

client = TestClient(main.app)


@pytest.fixture(autouse=True)
def reset_limiters():
    """Rate limiters are module-level state in main.py, shared across every
    test in this file (and across the whole process) unless reset --
    without this, a rate-limit-specific test would permanently consume
    budget that later tests in the same run depend on being available.
    """
    main.caller_limiter = SlidingWindowRateLimiter(
        limit=main.CALLER_LIMIT, window_seconds=main.CALLER_WINDOW_SECONDS
    )
    main.domain_limiter = SlidingWindowRateLimiter(
        limit=main.DOMAIN_LIMIT, window_seconds=main.DOMAIN_WINDOW_SECONDS
    )
    yield


def test_healthz():
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_diagnose_rejects_invalid_url_body():
    resp = client.post("/diagnose", json={"url": "not a url"})
    assert resp.status_code == 422  # pydantic validation, never reaches run_dry_check


def test_diagnose_rejects_missing_url_field():
    resp = client.post("/diagnose", json={})
    assert resp.status_code == 422


async def _fake_clean_report(url: str) -> DiagnosisReport:
    return DiagnosisReport(
        url=url,
        mode="dry",
        http_status=402,
        checks=[
            CheckResult(
                check_id="resource_present",
                status=Status.PASS,
                detail="ok",
                confidence=Confidence.SERVER,
            )
        ],
        verdict="No issues found in checks 1-4...",
    )


def test_diagnose_returns_report_on_success(monkeypatch):
    monkeypatch.setattr(main, "run_dry_check", _fake_clean_report)

    resp = client.post("/diagnose", json={"url": "https://api.example.com/data"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["http_status"] == 402
    assert body["checks"][0]["status"] == "pass"
    assert body["checks"][0]["confidence"] == "server-side"
    assert "No issues found" in body["verdict"]


def test_diagnose_maps_ssrf_blocked_to_400(monkeypatch):
    async def _raise_ssrf(url: str):
        raise SSRFBlocked("target resolves to a blocked address")

    monkeypatch.setattr(main, "run_dry_check", _raise_ssrf)

    resp = client.post("/diagnose", json={"url": "https://internal.example.com/x"})
    assert resp.status_code == 400
    # Deliberately doesn't leak *why* it was blocked -- see main.py's comment.
    assert "blocked" not in resp.json()["detail"].lower()


def test_diagnose_maps_fetch_error_to_502(monkeypatch):
    async def _raise_fetch_error(url: str):
        raise FetchError("timeout fetching target")

    monkeypatch.setattr(main, "run_dry_check", _raise_fetch_error)

    resp = client.post("/diagnose", json={"url": "https://slow.example.com/x"})
    assert resp.status_code == 502
    assert "timeout" in resp.json()["detail"].lower()


def test_diagnose_rate_limits_per_caller(monkeypatch):
    monkeypatch.setattr(main, "run_dry_check", _fake_clean_report)
    main.caller_limiter = SlidingWindowRateLimiter(limit=1, window_seconds=3600)
    # domain limiter left generous so only the caller limit is exercised
    main.domain_limiter = SlidingWindowRateLimiter(limit=1000, window_seconds=3600)

    first = client.post("/diagnose", json={"url": "https://one.example.com/data"})
    assert first.status_code == 200

    # Same caller (TestClient always presents the same client host), a
    # different target domain -- still blocked, because the limit here is
    # per caller, not per domain.
    second = client.post("/diagnose", json={"url": "https://two.example.com/data"})
    assert second.status_code == 429


def test_diagnose_rate_limits_per_target_domain(monkeypatch):
    monkeypatch.setattr(main, "run_dry_check", _fake_clean_report)
    main.caller_limiter = SlidingWindowRateLimiter(limit=1000, window_seconds=3600)
    main.domain_limiter = SlidingWindowRateLimiter(limit=1, window_seconds=3600)

    first = client.post("/diagnose", json={"url": "https://same.example.com/a"})
    assert first.status_code == 200

    second = client.post("/diagnose", json={"url": "https://same.example.com/b"})
    assert second.status_code == 429


def test_diagnose_logs_successful_submission(monkeypatch):
    monkeypatch.setattr(main, "run_dry_check", _fake_clean_report)
    calls = []
    monkeypatch.setattr(
        main,
        "log_submission",
        lambda url, **kwargs: calls.append((url, kwargs)),
    )

    resp = client.post("/diagnose", json={"url": "https://api.example.com/data"})
    assert resp.status_code == 200
    assert len(calls) == 1
    url, kwargs = calls[0]
    assert url == "https://api.example.com/data"
    assert kwargs["blocked"] is False


def test_diagnose_logs_blocked_submission_on_rate_limit(monkeypatch):
    monkeypatch.setattr(main, "run_dry_check", _fake_clean_report)
    main.caller_limiter = SlidingWindowRateLimiter(limit=0, window_seconds=3600)
    calls = []
    monkeypatch.setattr(
        main,
        "log_submission",
        lambda url, **kwargs: calls.append((url, kwargs)),
    )

    resp = client.post("/diagnose", json={"url": "https://api.example.com/data"})
    assert resp.status_code == 429
    assert len(calls) == 1
    assert calls[0][1]["blocked"] is True


def test_diagnose_maps_unexpected_exception_to_500_without_leaking_detail(monkeypatch):
    # TestClient re-raises server exceptions by default (useful for
    # debugging real bugs in other tests); this test is specifically about
    # the production 500 path, so it needs raise_server_exceptions=False to
    # actually exercise main.py's generic exception handler.
    no_raise_client = TestClient(main.app, raise_server_exceptions=False)

    async def _raise_unexpected(url: str):
        raise RuntimeError("some internal bug with a stack trace's worth of detail")

    monkeypatch.setattr(main, "run_dry_check", _raise_unexpected)

    resp = no_raise_client.post("/diagnose", json={"url": "https://api.example.com/data"})
    assert resp.status_code == 500
    assert "some internal bug" not in resp.json()["detail"]
