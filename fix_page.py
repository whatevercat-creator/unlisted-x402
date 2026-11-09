"""
/fix (Listing Fix) and /fix/sample pages for unlisted.sh.

The Listing Fix is a done-for-you, human-paid service (card via Stripe), not
an x402 route: nothing here touches /diagnose, payment.py or the paywall.
Static HTML, no user input rendered, no JavaScript, no external assets.

Everything a buyer reads about the offer lives in the constants and data
below, and the HTML page, the JSON offer (GET /fix with Accept:
application/json), the JSON-LD and llms.txt are all built from them, so they
can't drift apart.
"""

from __future__ import annotations

import json
import os
from html import escape
from typing import Any

# ---------------------------------------------------------------------------
# Stripe Payment Links. Paste the two links here (both must start with
# https://buy.stripe.com/), then set STRIPE_LINKS_SET = True so
# tests/test_fix_page.py checks them. While a link is empty, the page shows
# OPENS_LABEL and an email link instead of a buy button, and /fix/buy/*
# sends visitors back to /fix#pricing.
# ---------------------------------------------------------------------------
FIX_LINK_STANDARD = "https://buy.stripe.com/6oU14mdfnaxt8Of2LV1Nu03"
FIX_LINK_FOUNDING = "https://buy.stripe.com/cNi00i5MV5d91lNbir1Nu02"
STRIPE_LINKS_SET = True

# Founding offer: open until the first 3 clients have bought it.
FOUNDING_OPEN = True
# The name on the buyer's card statement (confirm in Stripe before deploy).
STATEMENT_NAME = "CRYPTO SENTIMENT"
OPENS_LABEL = "Opens Thu Nov 5"

PRICE_USD = 49
FOUNDING_PRICE_USD = 29
FOUNDING_CLIENTS = 3
MAX_ROUTES = 5
DELIVERY_BUSINESS_DAYS = 3
RECHECK_DAYS = 30
# Matches main.PER_REQUEST_PRICE_CAP's default (tests check it).
TEST_PAYMENT_CAP = "$0.05"

CONTACT_EMAIL = "hi@unlisted.sh"
CANONICAL_URL = "https://unlisted.sh/fix"
SAMPLE_URL = "https://unlisted.sh/fix/sample"
TERMS_URL = "https://unlisted.sh/fix#terms"
PRICING_URL = "https://unlisted.sh/fix#pricing"
GUIDE_URL = "https://unlisted.sh/guide"
FORGE_ALONE_URL = "https://forgealone.com"
FIX_UPDATED_ISO = "2026-11-06"
MAILTO = f"mailto:{CONTACT_EMAIL}?subject=Listing%20Fix"

# Unlisted's automated checks, as the code defines them (tests compare this
# with every check_id in diagnosis.py, probe.py, bazaar.py and paid_check.py).
# multiple_accepts_noted isn't counted: it's a note that only the first of
# several payment options was checked, not a check.
CHECKS_WITHOUT_PAYMENT = (
    "payment_required_header",
    "resource_present",
    "scheme_mismatch",
    "resource_url_match",
    "standard_payment_option",
    "bazaar_extension",
    "extensions_placement",
    "description_length",
    "route_template",
    "schema_external_refs",
    "tag_count",
    "description_screening",
    "probe_response",
    "bazaar_index_status",
    "listed_price_match",
    "search_visibility",
)
CHECKS_NEEDING_PAYMENT = ("settlement_echo", "extension_response")
CHECK_COUNT = len(CHECKS_WITHOUT_PAYMENT) + len(CHECKS_NEEDING_PAYMENT)

CATALOGS = (
    "Coinbase CDP Bazaar",
    "Agentic.market",
    "x402scan",
    "Circle Agent Marketplace",
    "x402-list.com",
)

NEVER_ASK_FOR = (
    "API keys",
    "passwords",
    "seed phrases",
    "wallet access",
    "repository access",
    "deploy access",
)

STATEMENT_LINE = (
    f"Your card statement shows {STATEMENT_NAME}. Our payment account is shared with our "
    "Crypto Sentiment API, also run by Forge Alone."
)

# Terms, in the order shown at /fix#terms. (lead, text)
TERMS = (
    (
        "What you get",
        "a written report on one x402 service (up to 5 paid routes) covering the agent catalogs "
        "it should appear in, the reason it is missing from each, and the exact change to make. "
        "Delivered by email from hi@unlisted.sh within 3 business days of payment. One free "
        "recheck within 30 days.",
    ),
    (
        "No listing guarantee",
        "each catalog (Coinbase CDP Bazaar, x402scan, Circle and others) decides on its own. We "
        "diagnose and recommend; you make the changes.",
    ),
    (
        "Test payments",
        "if a route priced up to $0.05 isn't indexed, we may make one real test payment to your "
        "endpoint from our own test wallet so the catalog has a settlement to index. For routes "
        "priced higher, we only pay if you ask us to and cover the amount.",
    ),
    (
        "What we never ask for",
        "API keys, passwords, wallet access, repository access or deploy access. We only read "
        "what is public: your 402 responses, your OpenAPI spec and the catalogs.",
    ),
    (
        "Refunds",
        "full refund if we can't deliver a report (for example, your endpoint is unreachable or "
        "isn't x402). Once the report is delivered, the fee covers the work, whatever the "
        "catalogs decide.",
    ),
    (
        "Privacy",
        "your details and report stay private. We never publish your service or results without "
        "your written permission.",
    ),
    ("Questions", "hi@unlisted.sh"),
    ("Card statement", STATEMENT_LINE[0].lower() + STATEMENT_LINE[1:]),
)


def faq(price: str) -> tuple[tuple[str, str], ...]:
    """(question, answer) pairs, plain text. The visible FAQ and the
    FAQPage JSON-LD are both built from this."""
    return (
        (
            "How fast do I get the report?",
            "Within 3 business days of payment, by email from hi@unlisted.sh.",
        ),
        (
            "What if you can't deliver?",
            "You get a full refund, for example if your endpoint is unreachable or isn't x402. "
            "Once the report is delivered, the fee covers the work, whatever the catalogs decide.",
        ),
        (
            "Do I need to give you access to anything?",
            "No. We never ask for API keys, passwords, wallet access, repository access or deploy "
            "access. We only read what is public: your 402 responses, your OpenAPI spec and the "
            "catalogs. You make every change yourself.",
        ),
        (
            "What's the test payment?",
            f"If a route priced up to {TEST_PAYMENT_CAP} isn't indexed, we may make one real test "
            "payment to your endpoint from our own test wallet, so CDP has a settlement to index.",
        ),
        (
            f"My route costs more than {TEST_PAYMENT_CAP}. What then?",
            "We only make a test payment to it if you ask us to and cover the amount. The rest of "
            "the report is the same.",
        ),
        (
            "Can my AI agent buy this?",
            "Not the Listing Fix: a human pays for it by card through Stripe, so an agent should "
            f"hand {CANONICAL_URL} to its operator. The {price} Check is different: an agent can "
            "pay for it itself over x402, in USDC on Base, with no signup.",
        ),
        (
            "What happens after I pay?",
            "At checkout you tell us your service, its paid route URLs (up to 5) and your stack. "
            "We check it read-only and, if needed, make one test payment. You get the written "
            "report within 3 business days. Apply the fixes, then ask for your free recheck "
            "within 30 days.",
        ),
        ("What will my card statement show?", STATEMENT_LINE),
    )


# ---------------------------------------------------------------------------
# Checkout
# ---------------------------------------------------------------------------


def stripe_link(plan: str) -> str:
    """The Payment Link for a plan, or "" when it isn't set (or isn't a
    Stripe Payment Link)."""
    link = FIX_LINK_FOUNDING if plan == "founding" else FIX_LINK_STANDARD
    return link if link.startswith("https://buy.stripe.com/") else ""


def primary_plan() -> str:
    return "founding" if FOUNDING_OPEN else "standard"


def _plan_label(plan: str) -> str:
    if plan == "founding":
        return f"Get the Listing Fix: ${FOUNDING_PRICE_USD} founding price"
    return f"Get the Listing Fix: ${PRICE_USD}"


def _buy_button(plan: str, *, secondary: bool = False) -> str:
    cls = "btn btn2" if secondary else "btn"
    if stripe_link(plan):
        return f'<a class="{cls}" href="/fix/buy/{plan}">{escape(_plan_label(plan))}</a>'
    return f'<a class="{cls}" href="{MAILTO}">Email {CONTACT_EMAIL} about the Listing Fix</a>'


def _checkout_buttons(extra: str = "") -> str:
    """The one buy button (founding while FOUNDING_OPEN, else standard),
    with "then $49" beside the founding one, then `extra` (more buttons) on
    the same row. /fix/buy/standard keeps working while it isn't shown.
    While the primary link is unset, the opening date goes under the row."""
    parts = [_buy_button(primary_plan())]
    if stripe_link(primary_plan()) and FOUNDING_OPEN:
        parts.append(f'<span class="then">then ${PRICE_USD}</span>')
    row = f'<div class="actions">{"".join(parts)}{extra}</div>'
    if not stripe_link(primary_plan()):
        row += f'\n<p class="opens">{OPENS_LABEL}. Until then, email us.</p>'
    return row


# ---------------------------------------------------------------------------
# JSON offer and JSON-LD
# ---------------------------------------------------------------------------

DESCRIPTION = (
    "A written report on why one x402 service (up to 5 paid routes) isn't found in the agent "
    "catalogs, and the exact change to make for each. Delivered by email within 3 business days, "
    "with one free recheck within 30 days."
)


def offer_json(*, price: str) -> dict[str, Any]:
    """The machine-readable offer, served at GET /fix with Accept: application/json."""
    return {
        "name": "Listing Fix",
        "description": DESCRIPTION,
        "scope": f"One x402 service, up to {MAX_ROUTES} paid routes.",
        "price_usd": PRICE_USD,
        "founding_price_usd": FOUNDING_PRICE_USD,
        "founding_open": FOUNDING_OPEN,
        "founding_terms": f"First {FOUNDING_CLIENTS} clients, in exchange for honest feedback.",
        "delivery_business_days": DELIVERY_BUSINESS_DAYS,
        "recheck_days": RECHECK_DAYS,
        "catalogs": list(CATALOGS),
        "listing_guarantee": False,
        "what_we_never_ask_for": list(NEVER_ASK_FOR),
        "how_to_buy": (
            f"A human pays by card at {PRICING_URL}; agents should hand this link to their operator."
        ),
        "agent_alternative": {
            "name": "Check",
            "endpoint": "POST https://unlisted.sh/diagnose",
            "price": price,
            "payment": "x402, USDC on Base, no signup",
            "docs": "https://unlisted.sh/llms.txt",
        },
        "sample_report_url": SAMPLE_URL,
        "terms_url": TERMS_URL,
        "contact": CONTACT_EMAIL,
    }


def _ld(data: dict[str, Any]) -> str:
    text = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return f'<script type="application/ld+json">{text}</script>'


def _json_ld(price: str) -> str:
    offers = [
        {
            "@type": "Offer",
            "name": "Listing Fix",
            "price": str(PRICE_USD),
            "priceCurrency": "USD",
            "url": PRICING_URL,
        }
    ]
    if FOUNDING_OPEN:
        offers.append(
            {
                "@type": "Offer",
                "name": f"Listing Fix, founding price (first {FOUNDING_CLIENTS} clients)",
                "price": str(FOUNDING_PRICE_USD),
                "priceCurrency": "USD",
                "url": PRICING_URL,
            }
        )
    service = {
        "@context": "https://schema.org",
        "@type": "Service",
        "name": "Listing Fix",
        "description": DESCRIPTION,
        "url": CANONICAL_URL,
        "provider": {
            "@type": "Organization",
            "name": "Unlisted",
            "url": "https://unlisted.sh",
            "email": CONTACT_EMAIL,
        },
        "offers": offers,
    }
    faq_page = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}}
            for q, a in faq(price)
        ],
    }
    return _ld(service) + "\n" + _ld(faq_page)


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

_STYLE = """
:root { --bg:#fafaf9; --fg:#1c1917; --muted:#57534e; --card:#fff; --line:#e7e5e4; --accent:#b45309; --on-accent:#fff; --code:#f5f5f4; }
@media (prefers-color-scheme: dark) { :root { --bg:#0c0a09; --fg:#f5f5f4; --muted:#a8a29e; --card:#1c1917; --line:#292524; --accent:#f59e0b; --on-accent:#1c1917; --code:#292524; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:16px/1.6 system-ui,-apple-system,Segoe UI,sans-serif; }
main { max-width:960px; margin:0 auto; padding:24px 16px 72px; }
main > :not(.paths) { max-width:720px; margin-left:auto; margin-right:auto; }
.skip { position:absolute; left:-9999px; top:8px; background:var(--card); color:var(--fg); padding:10px 14px; border-radius:6px; z-index:1; }
.skip:focus { left:16px; }
a { color:var(--accent); }
a:focus-visible, .btn:focus-visible { outline:3px solid var(--accent); outline-offset:3px; }
nav { font-size:0.95rem; margin-bottom:24px; }
nav a { text-decoration:none; font-weight:600; }
h1 { font-size:2.1rem; line-height:1.2; text-wrap:balance; margin-top:0; margin-bottom:12px; letter-spacing:-0.02em; }
.lede { font-size:1.12rem; color:var(--muted); margin-top:0; }
h2 { font-size:1.35rem; line-height:1.3; text-wrap:balance; margin-top:48px; margin-bottom:10px; scroll-margin-top:16px; }
h3 { font-size:1.05rem; margin-top:0; margin-bottom:4px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px 18px; margin-top:12px; margin-bottom:12px; }
.actions { margin-top:20px; margin-bottom:0; }
.btn { display:block; text-align:center; min-height:44px; padding:11px 18px; border-radius:8px; background:var(--accent); color:var(--on-accent); font-weight:600; text-decoration:none; margin-bottom:10px; }
.btn2 { background:transparent; color:var(--accent); border:2px solid var(--accent); }
.then { display:block; text-align:center; color:var(--muted); font-size:0.92rem; margin:-4px 0 10px; }
.opens { color:var(--muted); font-size:0.92rem; margin-top:0; margin-bottom:4px; }
.micro { color:var(--muted); font-size:0.92rem; margin-top:4px; }
.paths { display:grid; grid-template-columns:1fr; gap:12px; margin-top:12px; }
.paths .card { margin:0; display:flex; flex-direction:column; }
.paths .who { color:var(--muted); font-size:0.95rem; margin-top:0; }
.paths .cost { color:var(--accent); font-weight:700; margin:0 0 6px; }
.paths .go { margin-top:auto; padding-top:8px; }
.paths .go a { display:inline-block; min-height:44px; padding:10px 0; }
ul.plain { padding-left:20px; }
ul.plain li { margin-bottom:8px; }
.ex { display:block; color:var(--muted); font-size:0.93rem; }
ol.steps { padding-left:22px; }
ol.steps li { margin-bottom:8px; }
.scroll { overflow-x:auto; }
table { width:100%; border-collapse:collapse; font-size:0.93rem; margin:12px 0; }
th, td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
thead th { color:var(--muted); font-weight:600; }
tbody th { font-weight:600; }
.fit { display:grid; grid-template-columns:1fr; gap:12px; }
.price-row { display:flex; flex-wrap:wrap; align-items:baseline; gap:6px 16px; }
.big { font-size:2rem; font-weight:700; color:var(--fg); }
.was { color:var(--muted); }
.faq h3 { margin-top:20px; }
.faq p { margin-top:0; }
.terms p { margin-top:0; margin-bottom:10px; }
code { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:0.88rem; background:var(--code); padding:1px 5px; border-radius:4px; overflow-wrap:anywhere; }
footer { margin-top:56px; color:var(--muted); font-size:0.9rem; border-top:1px solid var(--line); padding-top:16px; }
footer a { display:inline-block; padding:6px 0; }
@media (min-width:600px) {
  .btn { display:inline-block; margin-right:10px; }
  .then { display:inline-block; margin:0 16px 10px 0; }
  .fit { grid-template-columns:1fr 1fr; }
}
@media (min-width:760px) { .paths { grid-template-columns:repeat(3,1fr); } }
@media (prefers-reduced-motion: reduce) { * { transition:none !important; animation:none !important; scroll-behavior:auto !important; } }
"""

_HEAD_COMMON = """<link rel="icon" href="/logo.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/logo.png">
<meta property="og:image" content="https://unlisted.sh/logo.png">
<meta name="twitter:card" content="summary">"""

TITLE = "Listing Fix: get your x402 API found | Unlisted"
META_DESCRIPTION = (
    "Your x402 API works but agents can't find it. The Listing Fix checks one service against "
    "the CDP Bazaar, x402scan and other agent catalogs, and sends a written report with the exact "
    "fixes within 3 business days. No keys or wallet access."
)

# Report sections, each with one line quoted from the sample report.
_REPORT_SECTIONS = (
    (
        "Summary",
        "what's wrong, in plain words, and what to do first.",
        "Both routes are listed and healthy, but a buyer searching in everyday words does not find them.",
    ),
    (
        "Catalog status, with timestamped evidence",
        "each catalog's listing entry next to your live 402, side by side.",
        "CDP Bazaar, read 2026-11-02 19:19:47 UTC: /history shows an old description. Neither "
        "route has tags or a service name.",
    ),
    (
        "Fixes in order",
        "the exact change for each, with code snippets in your stack.",
        "Exact change: set service_name and tags on each paid route's RouteConfig.",
    ),
    (
        "Description rewrite",
        'search words first (what it is, as a buyer would type it), then a "use when" line, '
        "under 500 characters, and your best 5 tags first.",
        "Crypto news sentiment for one coin: label, score and top 5 headlines. Use when an agent "
        "needs a current read on news sentiment for a coin before research or a trade.",
    ),
    (
        "Trust signals",
        "health, response time, unknown paths returning 404, prices in line with similar services and real paid calls.",
        'Health endpoint: GET /health returned 200 {"status":"ok"} in 0.24 s (median of 5).',
    ),
    (
        "Next steps and recheck",
        "what to do after the fixes, and what counts as fixed.",
        "It counts as fixed when both listings show the new description and 5 tags, and at least "
        'one route comes back for "crypto sentiment".',
    ),
)

# (catalog, how listing works, what we check)
_CATALOG_ROWS = (
    (
        "Coinbase CDP Bazaar",
        "A route is listed after a payment to it settles through CDP's facilitator with a valid "
        "<code>extensions.bazaar</code> declaration. Routes with no settlement for 30 days are removed.",
        "Your live 402 next to the listing, CDP's index status, and whether Bazaar search finds "
        "you for your own words.",
    ),
    (
        "Agentic.market",
        "Its cards are pulled from the Bazaar. Whether a card is enriched (description, category, "
        "icon) is decided by Coinbase; we never promise it.",
        "Whether your card is there and what it shows.",
    ),
    (
        "x402scan",
        "Reads your 402s and your OpenAPI spec. You register the service yourself, with a wallet sign-in.",
        "The OpenAPI fields it asks for: price and protocols on each paid route, input and output "
        "schemas, and free routes marked public.",
    ),
    (
        "Circle Agent Marketplace",
        "You apply with an intake form and Circle reviews it by hand. It needs a working 402 and "
        "a published OpenAPI spec, checks your service's health continuously and drops it when "
        "unreachable.",
        "Your 402, OpenAPI spec and uptime, so your form is ready to submit.",
    ),
    (
        "x402-list.com",
        "Confirms you own the domain with a token file at <code>/.well-known/x402list.txt</code>.",
        "Whether your service appears and what it shows.",
    ),
)


def _founding_note() -> str:
    return (
        f"${FOUNDING_PRICE_USD} founding price for the first {FOUNDING_CLIENTS} clients, then ${PRICE_USD}."
        if FOUNDING_OPEN
        else f"${PRICE_USD} for one service."
    )


def _pricing_card() -> str:
    if FOUNDING_OPEN:
        prices = (
            f'<div class="price-row"><span class="big">${FOUNDING_PRICE_USD}</span>'
            f'<span>founding price</span>'
            f'<span class="was">Regular price ${PRICE_USD}</span></div>'
            f"<p>The founding price is for the first {FOUNDING_CLIENTS} clients, in exchange for "
            "honest feedback and, if you're happy, a short testimonial you write yourself.</p>"
        )
    else:
        prices = f'<div class="price-row"><span class="big">${PRICE_USD}</span><span>per service</span></div>'
    return f"""<div class="card">
<h3>Listing Fix</h3>
{prices}
<ul class="plain">
<li>One x402 service, up to {MAX_ROUTES} paid routes</li>
<li>Written report by email within {DELIVERY_BUSINESS_DAYS} business days of payment</li>
<li>One free recheck within {RECHECK_DAYS} days</li>
<li>Paid by card through Stripe. <a href="#terms">Terms</a></li>
</ul>
<p class="micro">{escape(STATEMENT_LINE)}</p>
</div>"""


def fix_page(*, price: str, paid_price: str) -> str:
    """The /fix page. `price`/`paid_price` are the /diagnose prices."""
    price_h, paid_h = escape(price), escape(paid_price)
    sections = "\n".join(
        f"<li><b>{escape(name)}</b>: {escape(what)}"
        f'<span class="ex">Sample: &ldquo;{escape(example)}&rdquo;</span></li>'
        for name, what, example in _REPORT_SECTIONS
    )
    catalog_rows = "\n".join(
        f'<tr><th scope="row">{escape(name)}</th><td>{how}</td><td>{what}</td></tr>'
        for name, how, what in _CATALOG_ROWS
    )
    never = "".join(f"<li>{escape(item[0].upper() + item[1:])}</li>" for item in NEVER_ASK_FOR)
    faq_html = "\n".join(
        f"<h3>{escape(q)}</h3>\n<p>{escape(a)}</p>" for q, a in faq(price)
    )
    terms_html = "\n".join(f"<p><b>{escape(lead)}:</b> {escape(text)}</p>" for lead, text in TERMS)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(TITLE)}</title>
<meta name="description" content="{escape(META_DESCRIPTION)}">
<link rel="canonical" href="{CANONICAL_URL}">
<link rel="alternate" type="application/json" href="{CANONICAL_URL}">
{_HEAD_COMMON}
<meta property="og:type" content="website">
<meta property="og:title" content="Listing Fix: get your x402 API found">
<meta property="og:description" content="{escape(META_DESCRIPTION)}">
<meta property="og:url" content="{CANONICAL_URL}">
{_json_ld(price)}
<style>{_STYLE}</style></head>
<body>
<a class="skip" href="#main">Skip to content</a>
<main id="main">
<nav><a href="/">unlisted.sh</a> &rsaquo; Listing Fix</nav>

<h1>Your x402 API works. Agents just can&#x27;t find it.</h1>
<p class="lede">It&#x27;s often something silent: an <code>http://</code> resource URL where <code>https://</code> belongs, an extensions block in the wrong place, a description the Bazaar screens out, or missing OpenAPI fields. The Listing Fix finds it and tells you the exact change.</p>
<p>{escape(_founding_note())}</p>
{_checkout_buttons('<a class="btn btn2" href="/fix/sample">See a sample report</a>')}
<p class="micro">No keys or wallet access. Report in {DELIVERY_BUSINESS_DAYS} business days. Free recheck within {RECHECK_DAYS} days.</p>

<h2 id="paths">Which path fits you?</h2>
<div class="paths">
<div class="card"><h3>Free guide</h3>
<p class="who">For you if you&#x27;d rather fix it yourself.</p>
<p class="cost">Free</p>
<p>Every cause we know of, with the symptom and the fix for each.</p>
<p class="go"><a href="/guide">Read the free guide</a></p></div>
<div class="card"><h3>Check</h3>
<p class="who">For you, or your agent, if you want an instant answer for one route.</p>
<p class="cost">{price_h} per check &middot; {paid_h} with a real test payment</p>
<p>Your 402, your Bazaar declaration and CDP&#x27;s live index status. An agent can pay it itself over x402: USDC on Base, no signup.</p>
<p class="go"><a href="/">How to call the Check</a></p></div>
<div class="card"><h3>Listing Fix</h3>
<p class="who">For you if you want it done for you, with a written report.</p>
<p class="cost">${PRICE_USD}{f" (${FOUNDING_PRICE_USD} founding price)" if FOUNDING_OPEN else ""}</p>
<p>We check one service against the 5 catalogs we cover and send you the exact fixes, in order.</p>
<p class="go"><a href="#pricing">See pricing and what&#x27;s included</a></p></div>
</div>

<h2 id="what-you-get">What you get</h2>
<p>A written report on one x402 service, up to {MAX_ROUTES} paid routes, by email from {CONTACT_EMAIL}. The quoted lines come from <a href="/fix/sample">our sample report</a>.</p>
<ul class="plain">
{sections}
</ul>

<h2 id="how-it-works">How it works</h2>
<ol class="steps">
<li>Pay, and tell us your service, its paid route URLs and your stack at checkout.</li>
<li>We check it read-only: Unlisted&#x27;s {CHECK_COUNT} automated checks plus each catalog&#x27;s listing. If a route needs one, we make one test payment.</li>
<li>You get the written report within {DELIVERY_BUSINESS_DAYS} business days.</li>
<li>You apply the fixes and ask for your free recheck within {RECHECK_DAYS} days.</li>
</ol>

<h2 id="catalogs">Catalogs covered</h2>
<div class="scroll"><table>
<thead><tr><th scope="col">Catalog</th><th scope="col">How listing works</th><th scope="col">What we check</th></tr></thead>
<tbody>
{catalog_rows}
</tbody>
</table></div>

<h2 id="fit">Is this a fit?</h2>
<div class="fit">
<div class="card"><h3>Good fit</h3>
<ul class="plain">
<li>Your x402 endpoint takes payment, but agents can&#x27;t find it in the catalogs.</li>
<li>Your payments settle through CDP&#x27;s facilitator, or you&#x27;re willing to switch.</li>
<li>You want the exact changes written out, and you&#x27;ll make them yourself.</li>
</ul></div>
<div class="card"><h3>Not a fit</h3>
<ul class="plain">
<li>Your API doesn&#x27;t use x402 yet.</li>
<li>Your payments settle through your own or another facilitator and you won&#x27;t switch. Those never reach the CDP Bazaar, and we&#x27;ll tell you that for free: <a href="/guide#other-facilitator">payments through a different facilitator</a>.</li>
<li>You need a guaranteed listing. Each catalog decides on its own.</li>
</ul></div>
</div>

<h2 id="already-listed">Already listed?</h2>
<p>The Listing Fix also confirms that a listed service is healthy and that buyers can find it, and refreshes stale listing text.</p>

<h2 id="safety">Your safety</h2>
<p>We never ask for:</p>
<ul class="plain" id="never-ask">{never}</ul>
<p>We only read what is public: your 402 responses, your OpenAPI spec and the catalogs. You make every change yourself.</p>

<h2 id="pricing">Pricing</h2>
{_pricing_card()}
{_checkout_buttons()}

<h2 id="sample">Sample report</h2>
<p>See the real report we wrote for our own Crypto Sentiment API, before its fixes: <a href="/fix/sample">sample Listing Fix report</a>.</p>

<h2 id="faq">Questions</h2>
<div class="faq">
{faq_html}
</div>

<h2 id="terms">Terms</h2>
<div class="terms">
{terms_html}
</div>

<footer><a href="mailto:{CONTACT_EMAIL}">{CONTACT_EMAIL}</a> &middot; <a href="/guide">Free guide</a> &middot; <a href="/llms.txt">For AI agents: llms.txt</a><br>
Unlisted isn&#x27;t affiliated with Coinbase. Built by <a href="{FORGE_ALONE_URL}">Forge Alone</a>.</footer>
</main></body></html>"""


# ---------------------------------------------------------------------------
# /fix/sample
# ---------------------------------------------------------------------------

SAMPLE_MD_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "docs", "listing-fix", "sample-report-crypto-sentiment.md"
)
_SAMPLE_HTML_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "docs", "listing-fix", "sample-report-crypto-sentiment.html"
)
SAMPLE_LABEL = "Sample: our own Crypto Sentiment API, checked 2026-11-02, before its fixes."

_SAMPLE_STYLE = _STYLE + """
.label { background:var(--card); border:1px solid var(--line); border-left:3px solid var(--accent); border-radius:8px; padding:12px 16px; }
pre { background:var(--code); border-radius:8px; padding:14px; overflow-x:auto; font-size:0.86rem; }
pre code { background:none; padding:0; }
blockquote { margin:12px 0; padding:4px 16px; border-left:3px solid var(--line); color:var(--muted); }
li.todo { list-style:none; margin-left:-20px; }
h1 { font-size:1.8rem; }
"""


def sample_page() -> str:
    with open(_SAMPLE_HTML_PATH, encoding="utf-8") as f:
        body = f.read()
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sample Listing Fix report: Crypto Sentiment API | Unlisted</title>
<meta name="description" content="A real Listing Fix report: why our own Crypto Sentiment x402 API wasn't found by buyers in the CDP Bazaar, with timestamped evidence and the exact fixes.">
<link rel="canonical" href="{SAMPLE_URL}">
{_HEAD_COMMON}
<meta property="og:type" content="article">
<meta property="og:title" content="Sample Listing Fix report: Crypto Sentiment API">
<meta property="og:description" content="A real Listing Fix report, with timestamped evidence and the exact fixes.">
<meta property="og:url" content="{SAMPLE_URL}">
<style>{_SAMPLE_STYLE}</style></head>
<body>
<a class="skip" href="#main">Skip to content</a>
<main id="main">
<nav><a href="/">unlisted.sh</a> &rsaquo; <a href="/fix">Listing Fix</a> &rsaquo; sample</nav>
<p class="label">{escape(SAMPLE_LABEL)} <a href="/fix">Back to the Listing Fix</a></p>
{body}
<footer><a href="/fix">Get the Listing Fix for your service</a> &middot; <a href="mailto:{CONTACT_EMAIL}">{CONTACT_EMAIL}</a><br>
Unlisted isn&#x27;t affiliated with Coinbase. Built by <a href="{FORGE_ALONE_URL}">Forge Alone</a>.</footer>
</main></body></html>"""


# ---------------------------------------------------------------------------
# llms.txt and sitemap
# ---------------------------------------------------------------------------


def llms_section() -> str:
    founding = (
        f" Founding price ${FOUNDING_PRICE_USD} for the first {FOUNDING_CLIENTS} clients."
        if FOUNDING_OPEN
        else ""
    )
    return f"""## Listing Fix (done for you, paid by a human)

- What: a written report on one x402 service (up to {MAX_ROUTES} paid routes): why it's missing from each agent catalog ({", ".join(CATALOGS)}) and the exact change to make. No listing guarantee: each catalog decides.
- Price: ${PRICE_USD}.{founding} Paid by card through Stripe, not over x402.
- Delivery: by email from {CONTACT_EMAIL} within {DELIVERY_BUSINESS_DAYS} business days of payment. One free recheck within {RECHECK_DAYS} days.
- How to buy: a human pays at {PRICING_URL}. Agents: hand that link to your operator.
- Offer as JSON: GET {CANONICAL_URL} with Accept: application/json
- Page: {CANONICAL_URL}. Sample report: {SAMPLE_URL}
"""


SITEMAP_ENTRIES = (
    f"<url><loc>{CANONICAL_URL}</loc><lastmod>{FIX_UPDATED_ISO}</lastmod></url>\n"
    f"<url><loc>{SAMPLE_URL}</loc><lastmod>{FIX_UPDATED_ISO}</lastmod></url>\n"
)
