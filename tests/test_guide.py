import inspect

from fastapi.testclient import TestClient

import bazaar
import diagnosis
import guide
import main
import paid_check
import probe
import payment


def _client():
    return TestClient(main.create_app())


def test_guide_is_free_html_with_both_prices():
    resp = _client().get("/guide")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "<title>Why your x402 endpoint isn&#x27;t in the CDP Bazaar (and how to fix it) | Unlisted</title>" in resp.text
    assert "<h2>x402 endpoint not showing in Bazaar: quick triage</h2>" in resp.text
    assert "CDP Bazaar not indexing" in resp.text
    assert '<meta name="description"' in resp.text
    assert payment.DEFAULT_PRICE_USD in resp.text
    assert payment.DEFAULT_PAID_PRICE_USD in resp.text
    assert '<link rel="canonical" href="https://unlisted.sh/guide">' in resp.text


def test_guide_stays_free_with_paywall_active():
    from test_payment import _build_paid_client

    client, _ = _build_paid_client()
    assert client.get("/guide").status_code == 200
    assert client.get("/robots.txt").status_code == 200
    assert client.get("/sitemap.xml").status_code == 200


CAUSE_ANCHORS = ("no-payment", "other-facilitator", "extension-missing", "extension-placement", "description",
                 "http-resource", "resource-missing", "empty-body", "post-as-get",
                 "schema-refs", "wildcard", "stale", "probe-rejected", "collapsed", "dropped")


def test_guide_has_fifteen_anchored_causes():
    text = _client().get("/guide").text
    assert len(CAUSE_ANCHORS) == 15
    for number, anchor in enumerate(CAUSE_ANCHORS, start=1):
        assert f'<h2 id="{anchor}">{number}. ' in text, anchor
        assert f'href="#{anchor}"' in text
    assert "Rule out causes 1 to 15" in text


def test_guide_check_ids_exist_in_code():
    source = "".join(inspect.getsource(m) for m in (diagnosis, bazaar, paid_check, probe))
    for check_id in guide.REFERENCED_CHECK_IDS:
        assert f'"{check_id}"' in source, check_id


def test_guide_not_in_openapi_and_linked_from_home():
    client = _client()
    paths = client.get("/openapi.json").json()["paths"]
    for p in ("/guide", "/robots.txt", "/sitemap.xml"):
        assert p not in paths
    assert 'href="/guide"' in client.get("/").text


def test_robots_and_sitemap():
    client = _client()
    robots = client.get("/robots.txt")
    assert "User-agent: *\nAllow: /" in robots.text
    assert "Sitemap: https://unlisted.sh/sitemap.xml" in robots.text
    sitemap = client.get("/sitemap.xml")
    assert sitemap.headers["content-type"].startswith("application/xml")
    assert "<loc>https://unlisted.sh/guide</loc>" in sitemap.text


def test_logo_and_favicon_served_free():
    client = _client()
    for path, ctype in (("/logo.png", "image/png"), ("/favicon.ico", "image/png"),
                        ("/logo.svg", "image/svg+xml")):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert resp.headers["content-type"].startswith(ctype)
    assert client.get("/logo.png").content[:8] == b"\x89PNG\r\n\x1a\n"
    for page in ("/", "/guide"):
        assert '<link rel="icon" href="/logo.svg"' in client.get(page).text


def test_guide_covers_stuck_processing_honestly():
    text = _client().get("/guide").text
    assert 'id="stuck-processing"' in text
    assert text.count('href="#stuck-processing"') >= 2
    assert "no confirmed fix" in text
    assert "Unlisted can't make CDP index a route" in text


def test_llms_txt_is_free_plain_text_with_prices():
    from test_payment import _build_paid_client

    for client in (_client(), _build_paid_client()[0]):
        resp = client.get("/llms.txt")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/plain")
        assert resp.text.startswith("# Unlisted")
        assert payment.DEFAULT_PRICE_USD in resp.text
        assert payment.DEFAULT_PAID_PRICE_USD in resp.text
        assert "POST https://unlisted.sh/diagnose" in resp.text
    assert 'href="/llms.txt"' in _client().get("/").text


def test_get_diagnose_is_405_with_a_hint():
    from test_payment import _build_paid_client

    for client in (_client(), _build_paid_client()[0]):
        resp = client.get("/diagnose")
        assert resp.status_code == 405
        assert resp.headers["allow"] == "POST"
        assert "Use POST" in resp.json()["detail"]


def test_openapi_documents_the_402_and_price():
    op = _client().get("/openapi.json").json()["paths"]["/diagnose"]["post"]
    assert "402" in op["responses"]
    assert payment.DEFAULT_PRICE_USD in op["description"]
    assert "get" not in _client().get("/openapi.json").json()["paths"]["/diagnose"]


def test_openapi_auth_modes_for_discovery():
    from test_payment import _build_paid_client

    paid = _build_paid_client()[0].get("/openapi.json").json()
    op = paid["paths"]["/diagnose"]["post"]
    assert op["x-payment-info"] == {
        "price": {
            "mode": "dynamic",
            "currency": "USD",
            "min": payment.DEFAULT_PRICE_USD.lstrip("$"),
            "max": payment.DEFAULT_PAID_PRICE_USD.lstrip("$"),
        },
        "protocols": [{"x402": {}}],
    }
    assert "security" not in op
    assert paid["paths"]["/healthz"]["get"]["security"] == []
    assert "POST /diagnose" in paid["info"]["x-guidance"]

    # Without the paywall /diagnose is free, so it's declared public, not paid.
    free = _client().get("/openapi.json").json()
    assert "x-payment-info" not in free["paths"]["/diagnose"]["post"]
    assert free["paths"]["/diagnose"]["post"]["security"] == []
    assert free["paths"]["/healthz"]["get"]["security"] == []


def test_guide_links_official_checklist_first_and_is_dated():
    text = _client().get("/guide").text
    official = f'<a href="{guide.OFFICIAL_CHECKLIST_URL}">Get discovered (Bazaar)</a>'
    assert official in text
    assert text.index(official) < text.index('id="no-payment"')
    assert "Updated October 4, 2026" in text
    assert "<lastmod>2026-10-04</lastmod>" in _client().get("/sitemap.xml").text


def test_guide_cites_coinbase_confirmations():
    text = _client().get("/guide").text
    facilitator = text[text.index('id="other-facilitator"'):text.index('id="extension-missing"')]
    assert "https://github.com/coinbase/cdp-sdk/issues/827" in facilitator
    assert "only surfaces resources that settle through the CDP Facilitator" in facilitator
    assert "EXTENSION-RESPONSES" in text
    assert "500 characters or fewer" in text and "under about 500" not in text


def test_guide_curation_section_is_honest_about_scope():
    text = _client().get("/guide").text
    section = text[text.index('id="not-curated"'):text.index("<h2>Check all of it in one call</h2>")]
    assert "https://github.com/coinbase/cdp-sdk/issues/838" in section
    assert "<code>bazaar.curated</code>" in section
    assert "doesn't read agentic.market's <code>enriched</code> field" in section
    assert 'href="#not-curated"' in text


def test_guide_visit_logs_referrer_site_and_kind_only(caplog):
    import json
    import logging

    client = _client()
    with caplog.at_level(logging.INFO, logger="x402_doctor"):
        client.get(
            "/guide",
            headers={
                "referer": "https://www.google.com/search?q=x402+bazaar+not+indexed",
                "user-agent": "Mozilla/5.0 (Macintosh) Safari/605.1.15",
            },
        )
        client.get("/guide", headers={"user-agent": "Googlebot/2.1"})
    visits = [json.loads(r.message) for r in caplog.records if "guide_visit" in r.message]
    assert visits == [
        {"event": "guide_visit", "referrer": "google.com", "kind": "browser"},
        {"event": "guide_visit", "referrer": "direct", "kind": "bot"},
    ]
    # Nothing identifying: no search terms, no user-agent string, no address.
    assert "x402+bazaar" not in caplog.text and "Macintosh" not in caplog.text


def test_guide_and_llms_describe_schema_refs_and_probe_checks():
    client = _client()
    text = client.get("/guide").text
    refs = text[text.index('<h2 id="schema-refs">') : text.index('<h2 id="wildcard">')]
    assert "<code>schema_external_refs</code> check covers this" in refs
    assert "doesn't scan your schemas" not in refs
    probe_box = text[text.index('<h2 id="probe-rejected">') : text.index('<h2 id="collapsed">')]
    assert "<code>probe_response</code> check covers this" in probe_box
    llms = client.get("/llms.txt").text
    assert "schema_external_refs" in llms and "probe_response" in llms
    description = client.get("/openapi.json").json()["paths"]["/diagnose"]["post"]["description"]
    assert "schema_external_refs" in description and "probe_response" in description


def test_guide_covers_misplaced_extensions_and_bare_400():
    client = _client()
    text = client.get("/guide").text
    placement = text[text.index('<h2 id="extension-placement">') : text.index('<h2 id="description">')]
    assert "<code>extensions_placement</code> check covers this" in placement
    assert "next to <code>accepts</code>" in placement
    assert guide.X402_SPEC_V2_URL in placement
    probe_section = text[text.index('<h2 id="probe-rejected">') : text.index('<h2 id="collapsed">')]
    assert "A bare request or CDP's probe gets a 400" in probe_section
    assert "never see your price" in probe_section
    assert "validation is most likely running before the paywall" in probe_section
    assert "inside accepts" in text[: text.index("</head>")]
    llms = client.get("/llms.txt").text
    assert "extensions_placement" in llms and "input validation running before the paywall" in llms
    description = client.get("/openapi.json").json()["paths"]["/diagnose"]["post"]["description"]
    assert "extensions_placement" in description


def test_llms_txt_describes_charging_per_mode():
    llms = _client().get("/llms.txt").text
    assert "Check: a target that answers with something other than a 402 still gets a completed report (HTTP 200), so that call is charged." in llms
    assert "you are charged only when a test payment is attempted" in llms
    assert "the body is only {\"detail\": \"<reason> You were not charged. Run the $0.02 Check for the diagnosis.\"}" in llms
