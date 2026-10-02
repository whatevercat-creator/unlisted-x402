import inspect

from fastapi.testclient import TestClient

import bazaar
import diagnosis
import guide
import main
import paid_check
import payment


def _client():
    return TestClient(main.create_app())


def test_guide_is_free_html_with_both_prices():
    resp = _client().get("/guide")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "x402 endpoint not showing in the Bazaar" in resp.text
    assert payment.DEFAULT_PRICE_USD in resp.text
    assert payment.DEFAULT_PAID_PRICE_USD in resp.text
    assert '<link rel="canonical" href="https://unlisted.sh/guide">' in resp.text


def test_guide_stays_free_with_paywall_active():
    from test_payment import _build_paid_client

    client, _ = _build_paid_client()
    assert client.get("/guide").status_code == 200
    assert client.get("/robots.txt").status_code == 200
    assert client.get("/sitemap.xml").status_code == 200


def test_guide_has_ten_anchored_causes():
    text = _client().get("/guide").text
    for anchor in ("no-payment", "other-facilitator", "extension-missing", "description",
                   "http-resource", "resource-missing", "empty-body", "post-as-get",
                   "wildcard", "stale"):
        assert f'id="{anchor}"' in text
        assert f'href="#{anchor}"' in text


def test_guide_check_ids_exist_in_code():
    source = "".join(inspect.getsource(m) for m in (diagnosis, bazaar, paid_check))
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
