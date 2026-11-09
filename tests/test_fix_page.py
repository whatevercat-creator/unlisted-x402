import inspect
import json
import logging
import re
from html import escape, unescape

import pytest
from fastapi.testclient import TestClient

import bazaar
import diagnosis
import fix_page
import main
import paid_check
import probe

BROWSER = "Mozilla/5.0 (Macintosh) Safari/605.1.15"


def _client():
    return TestClient(main.create_app())


def _paid_client():
    from test_payment import _build_paid_client

    return _build_paid_client()[0]


def _html():
    return _client().get("/fix").text


def _ld_blocks(text):
    return [json.loads(b) for b in re.findall(r'<script type="application/ld\+json">(.*?)</script>', text, re.S)]


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------


def test_fix_is_free_html_never_402():
    for client in (_client(), _paid_client()):
        resp = client.get("/fix", headers={"accept": "text/html"})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert resp.headers["vary"] == "Accept"
    assert _paid_client().get("/fix/sample").status_code == 200


def test_fix_page_content():
    text = _html()
    assert text.count("<h1") == 1
    assert "<h1>Your x402 API works. Agents just can&#x27;t find it.</h1>" in text
    assert '<h2 id="terms">Terms</h2>' in text
    assert '<a class="skip" href="#main">' in text
    assert "<title>Listing Fix: get your x402 API found | Unlisted</title>" in text
    assert '<link rel="canonical" href="https://unlisted.sh/fix">' in text
    for tag in ('property="og:title"', 'property="og:description"', 'property="og:image"', 'name="twitter:card"', 'name="description"'):
        assert tag in text, tag
    assert "Unlisted isn&#x27;t affiliated with Coinbase." in text
    assert '<a href="https://forgealone.com">Forge Alone</a>' in text
    assert "No keys or wallet access. Report in 3 business days. Free recheck within 30 days." in text
    assert 'href="/fix/sample"' in text and 'href="/guide#other-facilitator"' in text
    for lead, body in fix_page.TERMS:
        assert f"<p><b>{escape(lead)}:</b> {escape(body)}</p>" in text, lead
    # Prices from the constants and from /diagnose's defaults.
    assert f"${fix_page.PRICE_USD}" in text and f"${fix_page.FOUNDING_PRICE_USD} founding price" in text
    assert "$0.02" in text and "$0.10" in text
    assert fix_page.STATEMENT_NAME in text
    # One verified check count, used once.
    assert f"Unlisted&#x27;s {fix_page.CHECK_COUNT} automated checks" in text
    # No external scripts, stylesheets or images.
    assert "<script src" not in text and 'rel="stylesheet"' not in text and "<img" not in text


def test_the_heading_order_is_logical():
    levels = [int(n) for n in re.findall(r"<h([1-6])", _html())]
    assert levels[0] == 1
    for prev, cur in zip(levels, levels[1:]):
        assert cur <= prev + 1


def test_no_stripe_link_while_links_unset(monkeypatch):
    monkeypatch.setattr(fix_page, "FIX_LINK_STANDARD", "")
    monkeypatch.setattr(fix_page, "FIX_LINK_FOUNDING", "")
    text = _html()
    assert "buy.stripe.com" not in text
    assert "/fix/buy/" not in text
    assert fix_page.OPENS_LABEL in text
    assert 'href="mailto:hi@unlisted.sh?subject=Listing%20Fix"' in text


def test_buttons_use_the_logging_redirects_once_links_set(monkeypatch):
    monkeypatch.setattr(fix_page, "FIX_LINK_STANDARD", "https://buy.stripe.com/test_standard")
    monkeypatch.setattr(fix_page, "FIX_LINK_FOUNDING", "https://buy.stripe.com/test_founding")
    monkeypatch.setattr(fix_page, "FOUNDING_OPEN", True)
    text = _html()
    assert "buy.stripe.com" not in text
    # While the founding offer is open, only its button shows, with
    # "then $49" beside it; the $49 button is hidden.
    assert ('href="/fix/buy/founding">Get the Listing Fix: $29 founding price</a>'
            '<span class="then">then $49</span>') in text
    assert "/fix/buy/standard" not in text
    assert fix_page.OPENS_LABEL not in text
    # Primary button in the hero and repeated after the pricing card.
    assert text.count('href="/fix/buy/founding"') == 2
    assert text.count('<span class="then">then $49</span>') == 2
    after_card = text[text.index('id="pricing"'):]
    after_card = after_card[after_card.index("</div>"):]
    assert 'href="/fix/buy/founding"' in after_card


def test_founding_closed_hides_founding_offer(monkeypatch):
    monkeypatch.setattr(fix_page, "FOUNDING_OPEN", False)
    monkeypatch.setattr(fix_page, "FIX_LINK_STANDARD", "https://buy.stripe.com/test_standard")
    monkeypatch.setattr(fix_page, "FIX_LINK_FOUNDING", "https://buy.stripe.com/test_founding")
    text = _html()
    assert "/fix/buy/founding" not in text
    assert 'href="/fix/buy/standard">Get the Listing Fix: $49</a>' in text
    assert text.count('href="/fix/buy/standard"') == 2
    assert 'class="then"' not in text
    offers = [b for b in _ld_blocks(text) if b["@type"] == "Service"][0]["offers"]
    assert [o["price"] for o in offers] == ["49"]
    assert fix_page.offer_json(price="$0.02")["founding_open"] is False


def test_standard_buy_link_works_while_founding_open(monkeypatch):
    monkeypatch.setattr(fix_page, "FOUNDING_OPEN", True)
    monkeypatch.setattr(fix_page, "FIX_LINK_STANDARD", "https://buy.stripe.com/test_standard")
    resp = _client().get("/fix/buy/standard", follow_redirects=False)
    assert resp.status_code == 302 and resp.headers["location"] == "https://buy.stripe.com/test_standard"


def test_page_copy():
    text = _html()
    assert '<p class="cost">$0.02 per check &middot; $0.10 with a real test payment</p>' in text
    assert '<a href="#pricing">See pricing and what&#x27;s included</a>' in text
    assert "against the 5 catalogs we cover" in text and len(fix_page._CATALOG_ROWS) == 5
    assert "prices in line with similar services" in text
    for gone in ("every catalog", "ordinary pricing", "See the Listing Fix price", "Or $0.10 with a real test payment"):
        assert gone not in text, gone
    # Every free recheck mention carries its window.
    plain = unescape(re.sub(r"<[^>]+>", " ", text))
    mentions = re.findall(r"free recheck[^.]*", plain, re.I)
    assert mentions and all(f"within {fix_page.RECHECK_DAYS} days" in m for m in mentions), mentions


@pytest.mark.skipif(not fix_page.STRIPE_LINKS_SET, reason="Stripe Payment Links not set yet (fix_page.STRIPE_LINKS_SET)")
def test_stripe_links_are_set():
    for link in (fix_page.FIX_LINK_STANDARD, fix_page.FIX_LINK_FOUNDING):
        assert link.startswith("https://buy.stripe.com/"), link
        assert len(link) > len("https://buy.stripe.com/")


# --------------------------------------------------------------------------
# JSON offer and JSON-LD
# --------------------------------------------------------------------------


def test_fix_json_offer():
    resp = _client().get("/fix", headers={"accept": "application/json"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    data = resp.json()
    for field in ("name", "description", "scope", "price_usd", "founding_price_usd", "founding_open",
                  "delivery_business_days", "recheck_days", "catalogs", "what_we_never_ask_for",
                  "how_to_buy", "agent_alternative", "terms_url", "contact"):
        assert field in data, field
    assert data["price_usd"] == fix_page.PRICE_USD == 49
    assert data["founding_price_usd"] == fix_page.FOUNDING_PRICE_USD == 29
    assert data["founding_open"] is fix_page.FOUNDING_OPEN
    assert data["delivery_business_days"] == 3 and data["recheck_days"] == 30
    assert data["terms_url"] == "https://unlisted.sh/fix#terms"
    assert data["contact"] == "hi@unlisted.sh"
    assert "agents should hand this link to their operator" in data["how_to_buy"]
    assert data["agent_alternative"]["price"] == "$0.02"
    assert "POST https://unlisted.sh/diagnose" == data["agent_alternative"]["endpoint"]


def test_browsers_get_html():
    resp = _client().get("/fix", headers={"accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8"})
    assert resp.headers["content-type"].startswith("text/html")
    assert _client().get("/fix").headers["content-type"].startswith("text/html")


def test_json_ld_parses_and_faq_matches_visible_faq():
    text = _html()
    blocks = _ld_blocks(text)
    service = [b for b in blocks if b["@type"] == "Service"][0]
    assert [o["price"] for o in service["offers"]] == ["49", "29"]
    assert all(o["priceCurrency"] == "USD" for o in service["offers"])
    faq_ld = [b for b in blocks if b["@type"] == "FAQPage"][0]
    ld_questions = [q["name"] for q in faq_ld["mainEntity"]]
    faq_section = text[text.index('<div class="faq">'):text.index('<h2 id="terms">')]
    visible = [unescape(q) for q in re.findall(r"<h3>(.*?)</h3>", faq_section)]
    assert ld_questions == visible
    assert 6 <= len(visible) <= 8
    for item in faq_ld["mainEntity"]:
        assert escape(item["acceptedAnswer"]["text"]) in faq_section


# --------------------------------------------------------------------------
# Redirects and logging
# --------------------------------------------------------------------------


def _events(caplog, name):
    return [json.loads(r.message) for r in caplog.records if f'"{name}"' in r.message]


@pytest.mark.parametrize("plan", ["founding", "standard"])
def test_buy_redirects_to_stripe_and_logs_one_click(monkeypatch, caplog, plan):
    monkeypatch.setattr(fix_page, "FIX_LINK_STANDARD", "https://buy.stripe.com/test_standard")
    monkeypatch.setattr(fix_page, "FIX_LINK_FOUNDING", "https://buy.stripe.com/test_founding")
    with caplog.at_level(logging.INFO, logger="x402_doctor"):
        resp = _client().get(
            f"/fix/buy/{plan}",
            headers={"referer": "https://www.google.com/search?q=x402+listing", "user-agent": BROWSER},
            follow_redirects=False,
        )
    assert resp.status_code == 302
    assert resp.headers["location"] == f"https://buy.stripe.com/test_{plan}"
    assert _events(caplog, "fix_checkout_click") == [
        {"event": "fix_checkout_click", "plan": plan, "referrer": "google.com", "kind": "browser"}
    ]
    assert "testclient" not in caplog.text and "x402+listing" not in caplog.text and "Macintosh" not in caplog.text


@pytest.mark.parametrize("plan", ["founding", "standard"])
def test_buy_falls_back_when_link_unset(monkeypatch, caplog, plan):
    monkeypatch.setattr(fix_page, "FIX_LINK_STANDARD", "")
    monkeypatch.setattr(fix_page, "FIX_LINK_FOUNDING", "")
    with caplog.at_level(logging.INFO, logger="x402_doctor"):
        resp = _client().get(f"/fix/buy/{plan}", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/fix#pricing"
    assert _events(caplog, "fix_checkout_click") == []


def test_founding_buy_falls_back_when_closed(monkeypatch):
    monkeypatch.setattr(fix_page, "FOUNDING_OPEN", False)
    monkeypatch.setattr(fix_page, "FIX_LINK_FOUNDING", "https://buy.stripe.com/test_founding")
    resp = _client().get("/fix/buy/founding", follow_redirects=False)
    assert resp.status_code == 302 and resp.headers["location"] == "/fix#pricing"


def test_fix_visit_logged_once_per_view(caplog):
    client = _client()
    with caplog.at_level(logging.INFO, logger="x402_doctor"):
        client.get("/fix", headers={"referer": "https://x402scan.com/some/path?q=1", "user-agent": BROWSER})
        client.get("/fix", headers={"accept": "application/json", "user-agent": "python-httpx/0.28"})
        client.get("/fix/sample")
    assert _events(caplog, "fix_visit") == [
        {"event": "fix_visit", "referrer": "x402scan.com", "kind": "browser"},
        {"event": "fix_visit", "referrer": "direct", "kind": "bot"},
    ]
    assert "testclient" not in caplog.text and "some/path" not in caplog.text


# --------------------------------------------------------------------------
# Sample page, sitemap, llms.txt, home, guide
# --------------------------------------------------------------------------


def test_sample_page_has_every_markdown_heading():
    resp = _client().get("/fix/sample")
    assert resp.status_code == 200
    text = resp.text
    with open(fix_page.SAMPLE_MD_PATH, encoding="utf-8") as f:
        headings = [line.lstrip("#").strip() for line in f if re.match(r"#{1,6} ", line)]
    assert len(headings) >= 10
    for heading in headings:
        assert f">{escape(heading, quote=False)}</h" in text, heading
    assert fix_page.SAMPLE_LABEL in text
    assert 'href="/fix"' in text
    assert text.count("<h1") == 1


def test_no_full_address_on_any_page():
    client = _client()
    for path in ("/", "/guide", "/fix", "/fix/sample"):
        assert not re.search(r"0x[0-9a-fA-F]{40}", client.get(path).text), path


def test_sitemap_llms_home_and_guide_link_fix():
    client = _client()
    sitemap = client.get("/sitemap.xml").text
    assert "<loc>https://unlisted.sh/fix</loc>" in sitemap
    assert "<loc>https://unlisted.sh/fix/sample</loc>" in sitemap
    llms = client.get("/llms.txt").text
    assert "https://unlisted.sh/fix" in llms and "## Listing Fix" in llms
    assert "Accept: application/json" in llms
    home = client.get("/").text
    assert 'Want it done for you? <a href="/fix">Listing Fix</a>' in home
    assert 'property="og:title"' in home and 'property="og:description"' in home
    assert 'Built by <a href="https://forgealone.com">Forge Alone</a>' in home
    assert 'href="/fix"' in client.get("/guide").text


def test_new_pages_not_in_openapi():
    paths = _client().get("/openapi.json").json()["paths"]
    for p in ("/fix", "/fix/sample", "/fix/buy/founding", "/fix/buy/standard"):
        assert p not in paths


# --------------------------------------------------------------------------
# Wording rules
# --------------------------------------------------------------------------

_PAGES = ("/", "/guide", "/fix", "/fix/sample")


def test_guarantee_only_in_negations():
    allowed = ("No listing guarantee", "need a guaranteed listing")
    client = _client()
    for path in _PAGES:
        text = client.get(path).text
        for allowed_phrase in allowed:
            text = text.replace(allowed_phrase, "")
        assert "guarantee" not in text.lower(), path


def test_seed_phrase_only_in_never_ask_list():
    client = _client()
    for path in _PAGES:
        text = client.get(path).text
        if path == "/fix":
            start = text.index('<ul class="plain" id="never-ask">')
            text = text[:start] + text[text.index("</ul>", start):]
        assert "seed phrase" not in text.lower(), path


# --------------------------------------------------------------------------
# Facts the page states, checked against the code
# --------------------------------------------------------------------------


def test_check_count_matches_the_code():
    source = "".join(inspect.getsource(m) for m in (diagnosis, probe, bazaar, paid_check))
    in_code = set(re.findall(r'check_id\s*=\s*"([a-z_]+)"', source))
    in_code |= set(re.findall(r'CHECK_ID\s*=\s*"([a-z_]+)"', source))
    in_code.discard("multiple_accepts_noted")  # a note, not a check
    listed = set(fix_page.CHECKS_WITHOUT_PAYMENT) | set(fix_page.CHECKS_NEEDING_PAYMENT)
    assert in_code == listed
    assert fix_page.CHECK_COUNT == 18
    assert set(fix_page.CHECKS_NEEDING_PAYMENT) == {"settlement_echo", "extension_response"}


def test_test_payment_cap_matches_the_code():
    assert float(fix_page.TEST_PAYMENT_CAP.lstrip("$")) == main.PER_REQUEST_PRICE_CAP
