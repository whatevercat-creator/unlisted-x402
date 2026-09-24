"""
Tests for main.py's Bazaar-lookup wiring (build-order step 9): the
X402_DOCTOR_ENABLE_BAZAAR_LOOKUP env var gate, explicit bazaar_client
override, and that main.run_dry_check is called with or without a
bazaar_client exactly as documented in create_app's docstring.

Deliberately monkeypatches main.run_dry_check rather than exercising a
real fetch (that's test_dry_check.py's job) -- this file is only about
whether main.py wires the right bazaar_client (or none) through to it,
mirroring test_main.py's own stated scope ("this file is about the HTTP
layer... on top of it").
"""

import pytest
from fastapi.testclient import TestClient

import bazaar
import main
from diagnosis import DiagnosisReport


def _make_capturing_fake():
    calls = []

    async def _fake(url, **kwargs):
        calls.append(kwargs)
        return DiagnosisReport(url=url, mode="dry", http_status=402, checks=[], verdict="ok")

    return _fake, calls


def test_bazaar_lookup_disabled_by_default(monkeypatch):
    monkeypatch.delenv("X402_DOCTOR_ENABLE_BAZAAR_LOOKUP", raising=False)
    app = main.create_app()
    fake, calls = _make_capturing_fake()
    monkeypatch.setattr(main, "run_dry_check", fake, raising=False)
    # create_app() closes over the module-level run_dry_check imported name
    # at *call* time inside the handler (it's referenced unqualified), so
    # patching main.run_dry_check before the request is enough -- same
    # pattern test_main.py already relies on.
    client = TestClient(app)

    resp = client.post("/diagnose", json={"url": "https://api.example.com/data"})
    assert resp.status_code == 200
    assert calls == [{}]  # no bazaar_client kwarg passed at all


def test_bazaar_lookup_disabled_when_env_var_explicitly_false(monkeypatch):
    monkeypatch.setenv("X402_DOCTOR_ENABLE_BAZAAR_LOOKUP", "false")
    built = []
    monkeypatch.setattr(bazaar, "build_bazaar_client", lambda: built.append(1) or object())

    app = main.create_app()
    fake, calls = _make_capturing_fake()
    monkeypatch.setattr(main, "run_dry_check", fake, raising=False)
    client = TestClient(app)

    resp = client.post("/diagnose", json={"url": "https://api.example.com/data"})
    assert resp.status_code == 200
    assert built == []
    assert calls == [{}]


def test_bazaar_lookup_enabled_via_env_var_builds_and_wires_client(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(bazaar, "build_bazaar_client", lambda: sentinel)
    monkeypatch.setenv("X402_DOCTOR_ENABLE_BAZAAR_LOOKUP", "true")

    app = main.create_app()
    fake, calls = _make_capturing_fake()
    monkeypatch.setattr(main, "run_dry_check", fake, raising=False)
    client = TestClient(app)

    resp = client.post("/diagnose", json={"url": "https://api.example.com/data"})
    assert resp.status_code == 200
    assert calls == [{"bazaar_client": sentinel}]


def test_explicit_bazaar_client_overrides_env_var(monkeypatch):
    monkeypatch.delenv("X402_DOCTOR_ENABLE_BAZAAR_LOOKUP", raising=False)
    monkeypatch.setattr(
        bazaar, "build_bazaar_client", lambda: pytest.fail("should not build a default client")
    )
    explicit = object()

    app = main.create_app(bazaar_client=explicit)
    fake, calls = _make_capturing_fake()
    monkeypatch.setattr(main, "run_dry_check", fake, raising=False)
    client = TestClient(app)

    resp = client.post("/diagnose", json={"url": "https://api.example.com/data"})
    assert resp.status_code == 200
    assert calls == [{"bazaar_client": explicit}]
