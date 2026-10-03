"""
Free /guide page for unlisted.sh: "Why your x402 endpoint isn't in the Bazaar".

Static HTML, no user input rendered, no paywall (only POST /diagnose is a
paid route). Each cause section names the Unlisted check_id that reports
it, so keep those ids in sync with diagnosis.py / bazaar.py / paid_check.py
(tests/test_guide.py asserts they exist).

Search phrases it targets (title, description, headings): "x402 endpoint
not showing in Bazaar", "CDP Bazaar not indexing", "x402
discovery not listed after payment", "extensions.bazaar missing".
"""

from __future__ import annotations

from html import escape

GUIDE_UPDATED = "October 3, 2026"
GUIDE_UPDATED_ISO = "2026-10-03"
# Coinbase's own discovery checklist, linked near the top as the official source.
OFFICIAL_CHECKLIST_URL = "https://docs.cdp.coinbase.com/x402/seller/get-discovered"
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


# Issue links used more than once in the page.
_CDP_830 = '<a href="https://github.com/coinbase/cdp-sdk/issues/830">cdp-sdk #830</a>'
_CDP_835 = '<a href="https://github.com/coinbase/cdp-sdk/issues/835">cdp-sdk #835</a>'


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
<title>Why your x402 endpoint isn&#x27;t in the CDP Bazaar (and how to fix it) | Unlisted</title>
<meta name="description" content="Why the CDP Bazaar isn't indexing your x402 endpoint: no settled payment yet, a missing extensions.bazaar, an http:// resource URL, a long description, external schema $refs, and more, each with its fix.">
<link rel="canonical" href="{CANONICAL_URL}">
<link rel="icon" href="/logo.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/logo.png">
<meta property="og:image" content="https://unlisted.sh/logo.png">
<meta property="og:type" content="article">
<meta property="og:title" content="Why your x402 endpoint isn&#x27;t in the CDP Bazaar (and how to fix it)">
<meta property="og:description" content="A free guide to why the CDP Bazaar isn't listing your x402 endpoint, and how to fix each cause.">
<meta property="og:url" content="{CANONICAL_URL}">
<script type="application/ld+json">{{"@context":"https://schema.org","@type":"TechArticle","headline":"Why your x402 endpoint isn't in the CDP Bazaar (and how to fix it)","dateModified":"{GUIDE_UPDATED_ISO}","url":"{CANONICAL_URL}","publisher":{{"@type":"Organization","name":"Unlisted","url":"https://unlisted.sh"}}}}</script>
<style>{_STYLE}</style></head>
<body><main>
<nav><a href="/">unlisted.sh</a> &rsaquo; guide</nav>

<h1>Why your x402 endpoint isn&#x27;t in the CDP Bazaar (and how to fix it)</h1>
<p class="meta">Updated {GUIDE_UPDATED} &middot; free guide by Unlisted</p>

<p class="lede">Your x402 endpoint returns a 402, takes payment, and works. But it isn't in the CDP Bazaar, so agents searching the catalog never find it. Below is every cause we know of, with the symptom and the fix for each. You can work through it by hand for free.</p>

<div class="card"><b>Official source.</b> Coinbase's <a href="{OFFICIAL_CHECKLIST_URL}">Get discovered (Bazaar)</a> guide has the checklist and troubleshooting notes this page builds on. The causes below add what sellers have run into, each linked to where it was reported or confirmed.</div>

<div class="card"><b>How a route gets listed.</b> The Bazaar doesn't crawl the web for x402 endpoints. A route gets listed when a payment to it <em>settles through CDP's facilitator</em>, its 402 challenge carries a valid <code>extensions.bazaar</code> declaration, and the settle request carries both <code>paymentPayload.extensions.bazaar</code> and <code>paymentPayload.resource</code>. CDP then probes the route with the example input from your declaration and expects a 402. So you need a well-formed challenge on a public https URL, CDP as the facilitator, and at least one settled payment that carries the metadata.</div>

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
<li><a href="#schema-refs">External $ref/$id in a schema, or paymentPayload.resource not sent</a></li>
<li><a href="#wildcard">The route uses a bare wildcard</a></li>
<li><a href="#stale">You changed price or metadata and the listing didn't update</a></li>
<li><a href="#probe-rejected">CDP's probe gets an error instead of a 402</a></li>
<li><a href="#collapsed">Several URLs collapse into one entry</a></li>
<li><a href="#dropped">The route dropped out after 30 days without a settlement</a></li>
</ol>
<p style="margin:10px 0 0">None of them fit? See <a href="#stuck-processing">accepted as &ldquo;processing&rdquo;, never indexed</a>. Listed but not featured? See <a href="#not-curated">indexed, but not curated (<code>enriched: false</code>)</a>.</p></div>

<h2>x402 endpoint not showing in Bazaar: quick triage</h2>
<div class="scroll"><table>
<tr><th>What you see</th><th>Most likely cause</th></tr>
<tr><td>Nobody has paid the route yet</td><td><a href="#no-payment">1</a></td></tr>
<tr><td>Real payments have landed, still not listed</td><td><a href="#other-facilitator">2</a>, then <a href="#extension-missing">3</a> and <a href="#description">4</a></td></tr>
<tr><td>Your app runs behind Render, Railway, Fly, Heroku, nginx or a load balancer</td><td><a href="#http-resource">5</a></td></tr>
<tr><td>Some x402 clients say there are no payment options</td><td><a href="#empty-body">7</a></td></tr>
<tr><td>CDP's facilitator answered <code>rejected</code></td><td><a href="#extension-missing">3</a> (read <code>rejectedReason</code>)</td></tr>
<tr><td>Valid challenge, settled through CDP, status &ldquo;processing&rdquo;, still not listed</td><td><a href="#probe-rejected">12</a> and <a href="#schema-refs">9</a>, then <a href="#stuck-processing">the open reports</a></td></tr>
<tr><td>Was listed, now gone</td><td><a href="#dropped">14</a></td></tr>
<tr><td>Several of your URLs show up as one entry</td><td><a href="#collapsed">13</a></td></tr>
<tr><td>Listed, but agentic.market shows <code>enriched: false</code></td><td><a href="#not-curated">Not curated</a></td></tr>
<tr><td>Listed, but with the wrong method or no input schema</td><td><a href="#post-as-get">8</a></td></tr>
<tr><td>Listed, but showing an old price or description</td><td><a href="#stale">11</a></td></tr>
</table></div>

<h2>CDP Bazaar not indexing your route? Ask it directly first</h2>
<p>CDP's discovery API is public. Open this in your browser with your receiving wallet address:</p>
<pre><code>https://api.cdp.coinbase.com/platform/v2/x402/discovery/merchant?payTo=0xYOUR_PAY_TO_ADDRESS</code></pre>
<p>If your route shows up there, it's listed. If the list is empty or your route is missing, read on.</p>
<p>Your own server can also read CDP's answer. On verify and settle, CDP's facilitator returns an <code>EXTENSION-RESPONSES</code> header: base64 JSON whose <code>bazaar.status</code> is <code>success</code> (cataloged), <code>processing</code> (accepted, being cataloged) or <code>rejected</code> (with a <code>rejectedReason</code>). No <code>bazaar</code> key means discovery wasn't submitted at all. <code>processing</code> doesn't confirm that indexing will succeed (<a href="{OFFICIAL_CHECKLIST_URL}#troubleshooting-discovery">Coinbase's troubleshooting notes</a>).</p>

<h2 id="no-payment">1. No payment has settled through CDP yet</h2>
<h3>Symptom</h3>
<p>Your challenge looks right, but the route has never been paid. This is the most common cause for a brand-new endpoint.</p>
<h3>Fix</h3>
<p>Make one real, paid call to the route that settles through CDP's facilitator. A cent is enough. Then give the Bazaar time to crawl it. It doesn't have to be a mainnet payment: a Coinbase contributor confirmed in {_CDP_835} that a testnet settlement through CDP works too, because the Bazaar indexes your live metadata, as long as <code>extensions.bazaar.info.input</code> and <code>paymentPayload.resource</code> are set.</p>
{_check_box("bazaar_index_status", f"the {price} Check asks CDP whether your route is indexed and, if it isn't, whether CDP's own simulation would accept it. &ldquo;Would be accepted&rdquo; means you're only missing the first payment. The {paid_price} Check + real payment (<code>?mode=paid</code>) makes that payment for you, on Base mainnet in USDC, for routes priced up to $0.05. Its result is in <code>settlement_echo</code>.")}

<h2 id="other-facilitator">2. Payments settle through a different facilitator</h2>
<h3>Symptom</h3>
<p>You've had real, settled payments, but the route still isn't listed.</p>
<h3>Fix</h3>
<p>The CDP Bazaar only learns about routes from settlements that go through CDP's facilitator. A Coinbase contributor confirmed this in <a href="https://github.com/coinbase/cdp-sdk/issues/827">cdp-sdk #827</a>: &ldquo;CDP Bazaar only surfaces resources that settle through the CDP Facilitator.&rdquo; If your server is set up with another facilitator, CDP never sees those payments. That includes the x402.org facilitator, which keeps its own separate catalog, not the CDP Bazaar. Point the route at CDP's facilitator (it needs a CDP API key), then make one more paid call.</p>
<div class="card check"><b>Check it with Unlisted:</b> Unlisted can't see which facilitator settled your past payments. If the report says &ldquo;would be accepted&rdquo; and you know payments have landed, this is the likely cause.</div>

<h2 id="extension-missing">3. <code>extensions.bazaar</code> is missing or malformed</h2>
<h3>Symptom</h3>
<p>Payments settle, but there's nothing for the Bazaar to index. Decode your 402 challenge, and either <code>extensions.bazaar</code> isn't there or it's incomplete.</p>
<h3>Fix</h3>
<p>Declare discovery metadata on the route. In the Python SDK that's <code>declare_discovery_extension(...)</code> passed as the route's <code>extensions</code>, plus registering <code>bazaar_resource_server_extension</code> on the resource server. At minimum, <code>extensions.bazaar.info.input.type</code> must be <code>"http"</code> or <code>"mcp"</code>. If you include <code>info.output</code>, it needs a <code>type</code> too.</p>
<p>Make the example match the schema. Coinbase's docs say rejections are usually strict JSON Schema validation: your declared <code>input</code> must validate against <code>schema.properties.input</code>.</p>
<p>Also check the paying side. The settle request has to carry <code>paymentPayload.extensions.bazaar</code>, so a client that drops the extension when it sends the payment leaves CDP with nothing to index (see <a href="https://github.com/x402-foundation/x402/issues/3557">x402 #3557</a>).</p>
{_check_box("bazaar_extension", "the Check decodes your challenge and validates the declaration's structure. It doesn't validate your example input against <code>schema.properties.input</code>, so if CDP's facilitator answers <code>rejected</code>, its <code>rejectedReason</code> is the place to look.")}

<h2 id="description">4. The description is too long</h2>
<h3>Symptom</h3>
<p>Everything else is right, but payments fail. Coinbase's docs say CDP's facilitator rejects verify and settle requests whose description is over 500 characters, and sellers have reported the failure being hard to trace (see <a href="https://github.com/x402-foundation/x402/issues/2993">x402 #2993</a>).</p>
<h3>Fix</h3>
<p>Keep the route's <code>description</code> to 500 characters or fewer. One or two sentences is plenty: what the endpoint does and when an agent should call it.</p>
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

<h2 id="schema-refs">9. External <code>$ref</code>/<code>$id</code> in a schema, or <code>paymentPayload.resource</code> not sent</h2>
<h3>Symptom</h3>
<p>Payments settle through CDP and CDP's validation says the route would be accepted, but it never appears in the catalog. Sellers in <a href="https://github.com/x402-foundation/x402/issues/3045">x402 #3045</a> traced this to two things on their side.</p>
<h3>Fix</h3>
<p><b>Inline your schemas.</b> If the input schema or <code>output.schema</code> in your Bazaar declaration has a <code>$ref</code> or <code>$id</code> pointing to an external URL, inline the referenced definitions and drop the external <code>$id</code>. A Coinbase contributor confirmed this cause in {_CDP_835}, where indexing failed with &ldquo;schema must not contain external $ref/$id references&rdquo;. In x402 #3045, a <code>$ref</code> in <code>output.schema</code> broke CDP's validator, and a seller who had fixed <code>output.schema</code> was still unlisted until they did the same for the input schema. CDP's <code>/v2/x402/validate</code> passed their route both times. After the fix and a new settlement, it was indexed.</p>
<p><b>Send <code>paymentPayload.resource</code> when you settle.</b> Coinbase's docs say the settlement that triggers indexing must set both <code>paymentPayload.extensions.bazaar</code> and <code>paymentPayload.resource</code>, and a maintainer said in x402 #3045 that settlement succeeds without them. Current SDKs fill it in. Hand-rolled settle code often doesn't. One seller found that adding it alone wasn't enough: they also had to settle with an x402 v2 payload, where <code>resource</code> is an object rather than a URL string.</p>
<div class="card check"><b>Check it with Unlisted:</b> Unlisted doesn't scan your schemas for external references, and it can't see what your server sends CDP when it settles. A &ldquo;would be accepted&rdquo; result from the {price} Check doesn't rule this cause out. After you fix it, the {paid_price} Check + real payment (<code>?mode=paid</code>) makes the fresh settlement you need.</div>

<h2 id="wildcard">10. The route uses a bare wildcard</h2>
<h3>Symptom</h3>
<p>The route is declared as <code>/prices/*</code>, so the listing can't tell agents what goes in the path.</p>
<h3>Fix</h3>
<p>Use a named parameter in the paywall's route pattern, such as <code>GET /prices/:symbol</code>, so the discovery metadata names the path parameter. With the CDP SDK's TypeScript <code>createX402Server</code>, the route key also needs a specific HTTP method: Coinbase's docs say a wildcard method doesn't give the SDK enough to generate discovery metadata.</p>
{_check_box("route_template", "when your challenge includes <code>routeTemplate</code>, the Check flags a bare <code>*</code> segment.")}

<h2 id="stale">11. You changed price or metadata and the listing didn't update</h2>
<h3>Symptom</h3>
<p>The route is listed, but with an old price, description or schema (see <a href="https://github.com/coinbase/cdp-sdk/issues/813">cdp-sdk #813</a>).</p>
<h3>Fix</h3>
<p>The Bazaar refreshes a route when it re-crawls it, so a change can take a while to show up. Make a new paid call after the change: a Coinbase contributor said in {_CDP_835} that indexing can't be retriggered on an existing settlement. Then check the crawl time again before assuming it's stuck. Ranking is separate: Coinbase's docs say it's recomputed every six hours.</p>
<div class="card check"><b>Check it with Unlisted:</b> when your route is indexed, the <code>bazaar_index_status</code> result includes when CDP last crawled it, plus 30-day calls and unique payers.</div>

<h2 id="probe-rejected">12. CDP's probe gets an error instead of a 402</h2>
<h3>Symptom</h3>
<p>A payment settled through CDP and the facilitator said <code>processing</code>, but the route never shows up. After a settlement, the Bazaar probes your route with the example input from <code>extensions.bazaar.info.input</code>, or an empty body if you didn't declare one. If your input validation runs before the x402 middleware, the probe gets a 400, 409 or 422 instead of a 402, and indexing stops there. A Coinbase contributor traced a stuck route to exactly this in {_CDP_830}: the probe got a 409.</p>
<h3>Fix</h3>
<p>Make sure the example input in your declaration passes your route's own validation and comes back as a 402 with the payment requirements in the <code>PAYMENT-REQUIRED</code> header. Or let the x402 middleware answer before validation runs. Then make a new settlement.</p>
<div class="card check"><b>Check it with Unlisted:</b> send your declaration's example input as <code>"body"</code> (with <code>"method": "POST"</code> for a POST route). If your route answers with anything but a 402, the {price} Check's report opens with &ldquo;Expected HTTP 402, got&rdquo; and the status it got. Unlisted probes with the body you send, not the example in your declaration, so use the same one.</div>

<h2 id="collapsed">13. Several URLs collapse into one entry</h2>
<h3>Symptom</h3>
<p>You serve many resources, such as <code>/data/0xabc&hellip;/report</code> and <code>/data/0x123&hellip;/report</code>, but the Bazaar lists them as a single entry.</p>
<h3>Fix</h3>
<p>Coinbase's docs say the Bazaar turns any path segment that is entirely a UUID, an EVM address or transaction hash, or a Solana address or transaction hash into a generic route parameter. To keep resources listed separately, add a prefix or suffix so the segment isn't a bare identifier, such as <code>/user-&lt;uuid&gt;</code> instead of <code>/&lt;uuid&gt;</code>.</p>
<div class="card check"><b>Check it with Unlisted:</b> Unlisted doesn't check this. Look up your payTo in the discovery API above to see how your routes were grouped.</div>

<h2 id="dropped">14. The route dropped out after 30 days without a settlement</h2>
<h3>Symptom</h3>
<p>Your route was listed, and now it's gone from the catalog and search results.</p>
<h3>Fix</h3>
<p>Coinbase's docs say resources that go 30 days without a settlement are removed, and endpoints that stop returning a 402 are eventually removed too. A listed route needs ongoing paid traffic through CDP's facilitator. Keep sending <code>paymentPayload.extensions.bazaar</code> and <code>paymentPayload.resource</code> on every settlement: a Coinbase contributor said in {_CDP_835} that this keeps your quality metrics aggregating and your route listed.</p>
{_check_box("bazaar_index_status", f"the {price} Check tells you whether CDP has the route indexed right now and, when it does, its 30-day calls and unique payers. The {paid_price} Check + real payment (<code>?mode=paid</code>) can make a settlement for routes priced up to $0.05.")}

<h2 id="stuck-processing">If none of these fit: accepted as &ldquo;processing&rdquo;, never indexed</h2>
<h3>Symptom</h3>
<p>Your challenge is valid, a payment settled through CDP's facilitator, and the facilitator answered with <code>bazaar.status: "processing"</code>. CDP's own validation says the route would be accepted. Days later it still isn't in the catalog.</p>
<h3>What's known</h3>
<p>Coinbase's docs say <code>processing</code> means the metadata was accepted and is being cataloged asynchronously, and that it doesn't confirm indexing will succeed. Routes that do get indexed see it too, so it tells you nothing either way.</p>
<p>Two of the reports have since been answered by a Coinbase contributor, and both turned out to be fixable on the seller's side: {_CDP_830} (CDP's probe got a 409, cause 12) and {_CDP_835} (an external <code>$id</code> in the schema, cause 9). As of {GUIDE_UPDATED}, <a href="https://github.com/x402-foundation/x402/issues/3266">x402 #3266</a> and <a href="https://github.com/x402-foundation/x402/issues/3281">x402 #3281</a> are still open with no maintainer answer, and there's no confirmed fix for them.</p>
<h3>What to try</h3>
<p>Rule out causes 1 to 14 first, especially 12 and 9, since several of them produce the same symptom. Then make one fresh settlement through CDP after your last change: indexing can't be retriggered on an existing settlement, and a testnet settlement works (cause 1). If it's still missing after a few days, add your route and settlement details to one of the open issues above. More reports make it easier for CDP to find the pattern.</p>
<div class="card check"><b>Check it with Unlisted:</b> the report's <code>bazaar_index_status</code> check tells you whether you're in this state: not indexed, but CDP would accept the route. Paid mode can make a fresh settlement for you. Unlisted can't make CDP index a route, and it won't claim to.</div>

<h2 id="not-curated">Indexed, but not curated (<code>enriched: false</code>)</h2>
<h3>Symptom</h3>
<p>Your route is in the CDP Bazaar, but agentic.market's record for your service shows <code>enriched: false</code>, often with an empty category or description, and you're not in the featured slice. This is a different problem from not being indexed (see <a href="https://github.com/coinbase/cdp-sdk/issues/838">cdp-sdk #838</a>).</p>
<h3>What it means</h3>
<p>A Coinbase contributor explained in #838 that <code>enriched</code> is only turned on for the hand-picked editorial set of curated endpoints. Meeting the published criteria makes an endpoint eligible to be selected, not entitled to it: selection is at Coinbase's discretion, based on service quality, category coverage and ecosystem fit.</p>
<p><a href="{OFFICIAL_CHECKLIST_URL}#requirements-for-curation">Coinbase's curation requirements</a> are: live x402 payments on mainnet, at least 99% availability over 30 days (above 99.5% gets priority), passing the platform health probe, agent-ready metadata (a complete input schema, a description that tells an agent when to use the endpoint, per-call pricing, supported networks and documented error responses), and passing validation. Curated endpoints that fail consecutive health probes are down-ranked, then dropped from the featured tier, and restored once they recover. The docs don't describe an application process.</p>
<div class="card check"><b>Check it with Unlisted:</b> the {price} Check's report includes <code>bazaar.curated</code>, read from CDP's discovery API (the payTo lookup above), which marks curated resources with <code>curated: true</code> and leaves the field out otherwise. It's <code>true</code> or <code>false</code>, or <code>null</code> when Unlisted couldn't tell: the lookup failed, or your route couldn't be matched in the listing. Unlisted doesn't read agentic.market's <code>enriched</code> field, and it can't get a route curated.</div>

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
<p>Replace the sample URL with your own endpoint. The report opens with <code>bazaar.indexed</code> (<code>true</code>, <code>false</code>, or <code>null</code> if CDP couldn't be reached) and <code>bazaar.curated</code> (<code>true</code>, <code>false</code>, or <code>null</code> if unknown), then one result per check with a fix for each failure.</p>

<footer><a href="/">unlisted.sh</a> &middot; <a href="/docs">API docs</a> &middot; <a href="/openapi.json">OpenAPI</a><br>
Unlisted isn't affiliated with Coinbase. &ldquo;CDP&rdquo; and &ldquo;Bazaar&rdquo; refer to Coinbase Developer Platform's x402 facilitator and discovery catalog.</footer>
</main></body></html>"""


def llms_txt(*, price: str, paid_price: str) -> str:
    """Plain-text summary for AI agents (llms.txt convention). Keep it in
    step with the /diagnose handler in main.py."""
    return f"""# Unlisted

> Unlisted tells you why an x402 endpoint isn't listed in the Coinbase CDP Bazaar. Pay per call in USDC on Base mainnet over x402. No signup and no API key: the payment is the auth.

## How to call it

- POST https://unlisted.sh/diagnose with a JSON body: {{"url": "<the x402 endpoint to diagnose>"}}
- Optional fields: "method" ("GET" or "POST", default "GET") for the target route, and "body" (a JSON object under 8 KB, POST targets only).
- An unpaid request returns HTTP 402. The payment requirements are in the PAYMENT-REQUIRED header and mirrored in the JSON body. Pay with x402 (scheme "exact", USDC, network eip155:8453) and retry the same request.
- GET /diagnose is not supported and returns 405. Use POST.
- Send a real endpoint URL. The sample api.example.com URL is rejected with a 400.

## Price

- Check: {price} per call. Reads the target's 402 challenge and Bazaar declaration, and asks CDP for its live index status.
- Check + real payment: {paid_price} per call, selected with ?mode=paid on the request URL. Also makes one real test payment to the target. Only for targets priced up to $0.05, and once per target domain per 24 hours.
- You are only charged when the check completes (HTTP 200). 4xx and 5xx responses are not charged.

## What you get back

- bazaar.indexed: true, false, or null if CDP could not be reached.
- bazaar.status: "indexed", "not_indexed_would_be_accepted", "not_indexed" or "unknown".
- bazaar.curated: true if CDP's discovery listing marks the route as Coinbase-curated, false if not, or null if unknown (lookup failed or the route couldn't be matched).
- checks: a list of results, each with check_id, status ("pass", "fail", "warn" or "skip"), detail and, for failures, a fix.
- verdict: a one-line summary.

## Limits

- Unlisted reports whether CDP would accept a route and can make a fresh settlement. It cannot make CDP index a route.
- Rate limits apply per caller and per target domain.

## More

- [Free guide: Why your x402 endpoint isn't in the CDP Bazaar (and how to fix it)](https://unlisted.sh/guide)
- [OpenAPI](https://unlisted.sh/openapi.json)
- Contact: hi@unlisted.sh
"""


ROBOTS_TXT = "User-agent: *\nAllow: /\nSitemap: https://unlisted.sh/sitemap.xml\n"

SITEMAP_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://unlisted.sh/</loc></url>
<url><loc>{CANONICAL_URL}</loc><lastmod>{GUIDE_UPDATED_ISO}</lastmod></url>
</urlset>
"""
