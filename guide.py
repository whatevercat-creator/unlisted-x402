"""
Free /guide page for unlisted.sh: "Why your x402 endpoint isn't in the Bazaar".

Static HTML, no user input rendered, no paywall (only POST /diagnose is a
paid route). Each cause section names the Unlisted check_id that reports
it, so keep those ids in sync with diagnosis.py / bazaar.py / paid_check.py
(tests/test_guide.py asserts they exist).

Search phrases it targets (title, description, headings): "x402 endpoint
not showing in Bazaar", "CDP Bazaar not indexing my endpoint", "x402
discovery not listed after payment", "extensions.bazaar missing".
"""

from __future__ import annotations

from html import escape

GUIDE_UPDATED = "October 1, 2026"
GUIDE_UPDATED_ISO = "2026-10-01"
CANONICAL_URL = "https://unlisted.sh/guide"

# check_ids this page refers to; tests verify each one is real.
REFERENCED_CHECK_IDS = (
    "bazaar_index_status",
    "settlement_echo",
    "bazaar_extension",
    "description_length",
    "scheme_mismatch",
    "resource_present",
    "route_template",
)

_STYLE = """
:root { --bg:#fafaf9; --fg:#1c1917; --muted:#57534e; --card:#fff; --line:#e7e5e4; --accent:#b45309; --code:#f5f5f4; }
@media (prefers-color-scheme: dark) { :root { --bg:#0c0a09; --fg:#f5f5f4; --muted:#a8a29e; --card:#1c1917; --line:#292524; --accent:#f59e0b; --code:#292524; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:16px/1.65 system-ui,-apple-system,Segoe UI,sans-serif; }
main { max-width:760px; margin:0 auto; padding:40px 16px 72px; }
nav { font-size:0.95rem; margin-bottom:28px; }
nav a { text-decoration:none; font-weight:600; }
h1 { font-size:2.1rem; line-height:1.2; margin:0 0 8px; letter-spacing:-0.02em; }
.meta { color:var(--muted); font-size:0.92rem; margin:0 0 24px; }
.lede { font-size:1.12rem; }
h2 { font-size:1.35rem; margin:44px 0 10px; line-height:1.3; scroll-margin-top:16px; }
h3 { font-size:1rem; margin:18px 0 4px; color:var(--muted); text-transform:uppercase; letter-spacing:0.04em; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px 18px; margin:16px 0; }
.check { border-left:3px solid var(--accent); }
.toc ol { margin:6px 0 0; padding-left:22px; }
table { width:100%; border-collapse:collapse; font-size:0.93rem; margin:12px 0; }
th, td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
th { color:var(--muted); font-weight:600; }
.scroll { overflow-x:auto; }
pre, code { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:0.86rem; }
pre { background:var(--code); border-radius:8px; padding:14px; overflow-x:auto; }
code { background:var(--code); padding:1px 5px; border-radius:4px; overflow-wrap:anywhere; }
pre code { background:none; padding:0; }
a { color:var(--accent); }
footer { margin-top:56px; color:var(--muted); font-size:0.9rem; border-top:1px solid var(--line); padding-top:16px; }
"""


def _check_box(check_id: str, text: str) -> str:
    return (
        f'<div class="card check"><b>Check it with Unlisted:</b> {text} '
        f"The report's <code>{escape(check_id)}</code> check covers this.</div>"
    )


def guide_page(*, price: str, paid_price: str) -> str:
    price, paid_price = escape(price), escape(paid_price)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>x402 endpoint not showing in the Bazaar? 10 causes and fixes | Unlisted</title>
<meta name="description" content="Why the CDP Bazaar isn't indexing your x402 endpoint: no settled payment yet, a missing extensions.bazaar, an http:// resource URL, a long description, and 6 more causes, each with its fix.">
<link rel="canonical" href="{CANONICAL_URL}">
<meta property="og:type" content="article">
<meta property="og:title" content="x402 endpoint not showing in the Bazaar? 10 causes and fixes">
<meta property="og:description" content="A free guide to why the CDP Bazaar isn't listing your x402 endpoint, and how to fix each cause.">
<meta property="og:url" content="{CANONICAL_URL}">
<script type="application/ld+json">{{"@context":"https://schema.org","@type":"TechArticle","headline":"x402 endpoint not showing in the Bazaar? 10 causes and fixes","dateModified":"{GUIDE_UPDATED_ISO}","url":"{CANONICAL_URL}","publisher":{{"@type":"Organization","name":"Unlisted","url":"https://unlisted.sh"}}}}</script>
<style>{_STYLE}</style></head>
<body><main>
<nav><a href="/">unlisted.sh</a> &rsaquo; guide</nav>

<h1>x402 endpoint not showing in the Bazaar? 10 causes and fixes</h1>
<p class="meta">Updated {GUIDE_UPDATED} &middot; free guide by Unlisted</p>

<p class="lede">Your x402 endpoint returns a 402, takes payment, and works. But it isn't in the CDP Bazaar, so agents searching the catalog never find it. Below is every cause we know of, with the symptom and the fix for each. You can work through it by hand for free.</p>

<div class="card"><b>How a route gets listed.</b> The Bazaar doesn't crawl the web for x402 endpoints. A route gets listed when a payment to it <em>settles through CDP's facilitator</em> and the 402 challenge carries a valid <code>extensions.bazaar</code> declaration. After that, CDP re-crawls it from time to time. So you need three things: a well-formed challenge, CDP as the facilitator, and at least one settled payment.</div>

<div class="card toc"><b>Causes</b>
<ol>
<li><a href="#no-payment">No payment has settled through CDP yet</a></li>
<li><a href="#other-facilitator">Payments settle through a different facilitator</a></li>
<li><a href="#extension-missing">extensions.bazaar is missing or malformed</a></li>
<li><a href="#description">The description is too long</a></li>
<li><a href="#http-resource">resource.url says http:// behind a proxy</a></li>
<li><a href="#resource-missing">The resource field is missing</a></li>
<li><a href="#empty-body">The 402 body is empty or accepts is malformed</a></li>
<li><a href="#post-as-get">A POST route is described as GET</a></li>
<li><a href="#wildcard">The route uses a bare wildcard</a></li>
<li><a href="#stale">You changed price or metadata and the listing didn't update</a></li>
</ol></div>

<h2>Quick triage</h2>
<div class="scroll"><table>
<tr><th>What you see</th><th>Most likely cause</th></tr>
<tr><td>Nobody has paid the route yet</td><td><a href="#no-payment">1</a></td></tr>
<tr><td>Real payments have landed, still not listed</td><td><a href="#other-facilitator">2</a>, then <a href="#extension-missing">3</a> and <a href="#description">4</a></td></tr>
<tr><td>Your app runs behind Render, Railway, Fly, Heroku, nginx or a load balancer</td><td><a href="#http-resource">5</a></td></tr>
<tr><td>Some x402 clients say there are no payment options</td><td><a href="#empty-body">7</a></td></tr>
<tr><td>Listed, but with the wrong method or no input schema</td><td><a href="#post-as-get">8</a></td></tr>
<tr><td>Listed, but showing an old price or description</td><td><a href="#stale">10</a></td></tr>
</table></div>

<h2>Free first step: ask the Bazaar directly</h2>
<p>CDP's discovery API is public. Open this in your browser with your receiving wallet address:</p>
<pre><code>https://api.cdp.coinbase.com/platform/v2/x402/discovery/merchant?payTo=0xYOUR_PAY_TO_ADDRESS</code></pre>
<p>If your route shows up there, it's listed. If the list is empty or your route is missing, read on.</p>

<h2 id="no-payment">1. No payment has settled through CDP yet</h2>
<h3>Symptom</h3>
<p>Your challenge looks right, but the route has never been paid. This is the most common cause for a brand-new endpoint.</p>
<h3>Fix</h3>
<p>Make one real, paid call to the route that settles through CDP's facilitator. A cent is enough. Then give the Bazaar time to crawl it.</p>
{_check_box("bazaar_index_status", f"the {price} Check asks CDP whether your route is indexed and, if it isn't, whether CDP's own simulation would accept it. &ldquo;Would be accepted&rdquo; means you're only missing the first payment. The {paid_price} Check + real payment (<code>?mode=paid</code>) makes that payment for you, on Base mainnet in USDC, for routes priced up to $0.05. Its result is in <code>settlement_echo</code>.")}

<h2 id="other-facilitator">2. Payments settle through a different facilitator</h2>
<h3>Symptom</h3>
<p>You've had real, settled payments, but the route still isn't listed.</p>
<h3>Fix</h3>
<p>The CDP Bazaar only learns about routes from settlements that go through CDP's facilitator. If your server is set up with another facilitator, CDP never sees those payments. Point the route at CDP's facilitator (it needs a CDP API key), then make one more paid call.</p>
<div class="card check"><b>Check it with Unlisted:</b> Unlisted can't see which facilitator settled your past payments. If the report says &ldquo;would be accepted&rdquo; and you know payments have landed, this is the likely cause.</div>

<h2 id="extension-missing">3. <code>extensions.bazaar</code> is missing or malformed</h2>
<h3>Symptom</h3>
<p>Payments settle, but there's nothing for the Bazaar to index. Decode your 402 challenge, and either <code>extensions.bazaar</code> isn't there or it's incomplete.</p>
<h3>Fix</h3>
<p>Declare discovery metadata on the route. In the Python SDK that's <code>declare_discovery_extension(...)</code> passed as the route's <code>extensions</code>, plus registering <code>bazaar_resource_server_extension</code> on the resource server. At minimum, <code>extensions.bazaar.info.input.type</code> must be <code>"http"</code> or <code>"mcp"</code>. If you include <code>info.output</code>, it needs a <code>type</code> too.</p>
<p>Also check the paying side. A client that drops the extension when it sends the payment leaves CDP with nothing to index (see <a href="https://github.com/x402-foundation/x402/issues/3557">x402 #3557</a>).</p>
{_check_box("bazaar_extension", "the Check decodes your challenge and validates the declaration.")}

<h2 id="description">4. The description is too long</h2>
<h3>Symptom</h3>
<p>Everything else is right, payments may even fail, and nothing tells you why. A long route description can break things without any error (see <a href="https://github.com/x402-foundation/x402/issues/2993">x402 #2993</a>).</p>
<h3>Fix</h3>
<p>Keep the route's <code>description</code> under about 500 characters. One or two sentences is plenty: what it returns and what it costs.</p>
{_check_box("description_length", "the Check measures your description against the limit.")}

<h2 id="http-resource">5. <code>resource.url</code> says <code>http://</code> behind a proxy</h2>
<h3>Symptom</h3>
<p>Your public URL is <code>https://</code>, but the decoded challenge advertises <code>http://</code>. Your host terminates TLS and forwards plain HTTP to your app, so the app builds the URL from what it sees. CDP's validation only accepts <code>https://</code> resource URLs.</p>
<h3>Fix</h3>
<p>Tell your server to trust the forwarded scheme. For uvicorn:</p>
<pre><code>uvicorn main:app --proxy-headers --forwarded-allow-ips='*'
# or set the env var
FORWARDED_ALLOW_IPS=*</code></pre>
<p>For other stacks, read <code>X-Forwarded-Proto</code> (Express: <code>app.set("trust proxy", true)</code>), or hardcode your public https base URL.</p>
{_check_box("scheme_mismatch", "the Check compares the scheme your challenge advertises with the one it was served over.")}

<h2 id="resource-missing">6. The <code>resource</code> field is missing</h2>
<h3>Symptom</h3>
<p>The challenge has payment options but no <code>resource</code>, so the Bazaar doesn't know which URL it's cataloging.</p>
<h3>Fix</h3>
<p>Include <code>resource.url</code>: the full public https URL of the paid route. Current x402 SDKs fill it in for you. Hand-rolled 402 responses often leave it out.</p>
{_check_box("resource_present", "the Check confirms the field is there and shows the URL it found.")}

<h2 id="empty-body">7. The 402 body is empty or <code>accepts</code> is malformed</h2>
<h3>Symptom</h3>
<p>x402 v2 puts the challenge in a base64 <code>PAYMENT-REQUIRED</code> header, and some servers send <code>{{}}</code> as the body. Clients and crawlers that read the body find no payment options.</p>
<h3>Fix</h3>
<p>Keep the header, and also return the same decoded JSON as the 402 body. Make sure <code>accepts</code> is a non-empty array of objects, each with <code>scheme</code>, <code>network</code>, <code>asset</code>, <code>amount</code> and <code>payTo</code>.</p>
<div class="card check"><b>Check it with Unlisted:</b> the Check reads the header first and falls back to the body. If it can't find a usable challenge in either, the report says so in <code>parse_error</code>.</div>

<h2 id="post-as-get">8. A POST route is described as GET</h2>
<h3>Symptom</h3>
<p>Your route takes a JSON body, but the listing (or the validation) treats it as GET, so it sends no body and gets an error instead of a 402.</p>
<h3>Fix</h3>
<p>Declare the body in the discovery metadata. In the Python SDK, pass <code>body_type="json"</code> plus an example <code>input</code> and <code>input_schema</code> to <code>declare_discovery_extension</code>. Make sure the route key says <code>POST</code> too.</p>
<div class="card check"><b>Check it with Unlisted:</b> send <code>"method": "POST"</code> and a sample <code>"body"</code>. Unlisted probes, pays and asks CDP using the route's real method.</div>

<h2 id="wildcard">9. The route uses a bare wildcard</h2>
<h3>Symptom</h3>
<p>The route is declared as <code>/prices/*</code>, so the listing can't tell agents what goes in the path.</p>
<h3>Fix</h3>
<p>Use a named parameter in the paywall's route pattern, such as <code>GET /prices/:symbol</code>, so the discovery metadata names the path parameter.</p>
{_check_box("route_template", "when your challenge includes <code>routeTemplate</code>, the Check flags a bare <code>*</code> segment.")}

<h2 id="stale">10. You changed price or metadata and the listing didn't update</h2>
<h3>Symptom</h3>
<p>The route is listed, but with an old price, description or schema (see <a href="https://github.com/coinbase/cdp-sdk/issues/813">cdp-sdk #813</a>).</p>
<h3>Fix</h3>
<p>The Bazaar refreshes a route when it re-crawls it, so a change can take a while to show up. Make a new paid call after the change, then check the crawl time again before assuming it's stuck.</p>
<div class="card check"><b>Check it with Unlisted:</b> when your route is indexed, the <code>bazaar_index_status</code> result includes when CDP last crawled it, plus 30-day calls and unique payers.</div>

<h2>Check all of it in one call</h2>
<p>Unlisted runs every check above against your endpoint and asks CDP for its live index status. No signup and no API key: you pay per call in USDC on Base through x402, and you're only charged if the check completes.</p>
<pre><code>POST https://unlisted.sh/diagnose
Content-Type: application/json

{{"url": "https://your-api.example.com/paid-route"}}</code></pre>
<div class="scroll"><table>
<tr><th>Mode</th><th>Price</th><th>What it does</th></tr>
<tr><td>Check</td><td>{price}</td><td>Your 402 challenge, the Bazaar declaration, and CDP's live index status</td></tr>
<tr><td>Check + real payment (<code>?mode=paid</code>)</td><td>{paid_price}</td><td>All of the above, plus one real test payment to your route. Once per domain per 24 hours.</td></tr>
</table></div>
<p>Replace the sample URL with your own endpoint. The report opens with <code>bazaar.indexed</code> (<code>true</code>, <code>false</code>, or <code>null</code> if CDP couldn't be reached), then one result per check with a fix for each failure.</p>

<footer><a href="/">unlisted.sh</a> &middot; <a href="/docs">API docs</a> &middot; <a href="/openapi.json">OpenAPI</a><br>
Unlisted isn't affiliated with Coinbase. &ldquo;CDP&rdquo; and &ldquo;Bazaar&rdquo; refer to Coinbase Developer Platform's x402 facilitator and discovery catalog.</footer>
</main></body></html>"""


ROBOTS_TXT = "User-agent: *\nAllow: /\nSitemap: https://unlisted.sh/sitemap.xml\n"

SITEMAP_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://unlisted.sh/</loc></url>
<url><loc>{CANONICAL_URL}</loc><lastmod>{GUIDE_UPDATED_ISO}</lastmod></url>
</urlset>
"""
