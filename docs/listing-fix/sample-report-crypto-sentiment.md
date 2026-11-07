Unlisted ran 15 checks when this report was written; it now runs 16 (payment_required_header was added on Nov 6, 2026).

# Listing Fix report: Crypto Sentiment API

Service: https://crypto-sentiment-x402.onrender.com
Paid routes checked: GET /sentiment/:symbol ($0.01), GET /history/:symbol ($0.05)
Report written: 2026-11-02 19:26 UTC

How this was checked: read-only. No payments, no signing and no browser. No access to the seller's code, except in fix 4, where the owner's code was read to settle one question. Anything that could not be checked that way is marked "not checked" with the reason.

## Summary

Both routes are listed and healthy, but a buyer searching in everyday words does not find them.
Neither route came back for "crypto sentiment", "bitcoin news sentiment" or "fear and greed", and the listings have no tags while nearly all the results that do come back have them.
First add tags and rewrite both descriptions, then make one paid call on each route ($0.06 in total) so the listings update.

## Do this

- [ ] Fix 1: add tags and a service name to both routes.
- [ ] Fix 2: rewrite both descriptions.
- [ ] Fix 3: publish an output schema in OpenAPI.
- [ ] Fix 4: make every place say too little recent news is a 422.
- [ ] Fix 5: tidy the smaller mismatches.
- [ ] Deploy fixes 1 to 5 and check they are live.
- [ ] Last, make the paid calls: one on /sentiment/BTC and one on /history/BTC ($0.06 in total).

## Catalog status

| Catalog | Read at (UTC) | Listed? | What buyers see | Problem |
| --- | --- | --- | --- | --- |
| CDP Bazaar | 2026-11-02 19:19:47 | Yes, both routes | /sentiment: last called 2026-10-29 19:49 UTC, 5 calls from 3 payers in 30 days. /history: last called 2026-10-18 19:18 UTC, 3 calls from 2 payers in 30 days. | /history shows an old description. Neither route has tags or a service name. |
| Bazaar search | 2026-11-02 19:20:07, repeated 19:23:36 | Not returned for any of the 3 phrases, on either read | "crypto sentiment": 15 other results. "bitcoin news sentiment": 14 other results. "fear and greed": 20 other results (the most this search returns). | Buyers using these words do not see this service. |
| x402scan | 2026-11-02 19:21:15 | Not checked | Unknown | Cannot be checked read-only for free. See "Not checked" at the end. |
| Agentic.market | 2026-11-02 19:23:10 | Card present, not enriched | Card name is the bare hostname. Service description, category, icon and tags are empty. Both routes and prices are correct. | The card shows no service-level description or category. |

## Fixes in order

### 1. Add tags and a service name to both routes

- About how long: about 10 minutes
- Evidence: both Bazaar listings have no tags and no service name. Of the results returned for "crypto sentiment", 15 of 15 have tags, and for "bitcoin news sentiment", 14 of 14. For "fear and greed" it is 14 of 20. This shows a pattern, not proof: the ranking is CDP's and it is not public. Tags are the cheapest thing to change, and the x402 SDK describes them as "short topical tags for discovery search".
- Exact change: set `service_name` and `tags` on each paid route's `RouteConfig`. The x402 Python SDK 2.25.0 copies both into `resource` in the 402. The Bazaar keeps the first 5 tags, so put the best tag first.
- Snippet:

```python
RouteConfig(
    # ...keep the existing accepts, description, mime_type and extensions...
    service_name="Crypto Sentiment API",  # 32 characters or fewer
    tags=["crypto sentiment", "bitcoin sentiment", "news sentiment", "fear and greed", "crypto news"],
)
```

### 2. Rewrite both descriptions in the words buyers use

- About how long: about 10 minutes
- Evidence: neither description has a "use when" line. Both start with detail rather than the plain words a buyer types. "/sentiment" spends nearly a quarter of its 486 characters naming outlets. The routes do rank when searched with their own opening words (rank 5 for /sentiment, rank 7 for /history), so the listing is searchable and the words are the likely gap.
- Exact change: replace `description` on each route's `RouteConfig` with the rewritten text in "Description review" below. Both versions passed Unlisted's length, tag and wording checks.
- Snippet: see "Description review".

### 3. Publish an output schema in OpenAPI

- About how long: about 30 minutes
- Evidence: in /openapi.json, the 200 response of both paid routes has `"schema": {}`, which is an empty schema. There is an example next to it, but no field definitions. x402scan's discovery guide lists "Input/Output Schema Missing" as a reason an operation fails, and its sample shows a real schema. The schema in `extensions.bazaar` is complete and the Bazaar listing already carries it, so this fix is for x402scan and for anyone reading the OpenAPI file.
- Exact change: give both routes a typed response model, or set `responses.200.content.application/json.schema` by hand. The field definitions can be copied from `extensions.bazaar.schema.properties.output.properties.example` in the live 402.
- Snippet (/sentiment, shortened):

```json
"200": {
  "content": {
    "application/json": {
      "schema": {
        "type": "object",
        "properties": {
          "symbol": {"type": "string"},
          "overall_sentiment": {"type": "object"},
          "drivers": {"type": "array", "maxItems": 5, "items": {"type": "object"}},
          "drivers_summary": {"type": "string"}
        },
        "required": ["symbol", "overall_sentiment"]
      }
    }
  }
}
```

### 4. Settle what happens when there is too little recent news

- About how long: about 20 minutes
- Evidence: the service's own text disagrees. llms.txt, the /sentiment route notes in OpenAPI and `info.description` say this case is refused with HTTP 422 and not charged. The live 402 description, `info.x-guidance`, the OpenAPI 200 description and the output schema's label list say the reply is a label of "insufficient recent news". The home page says it too: `app/home.py` line 201 says low-coverage coins "often get 'insufficient recent news' (not charged)", and line 246 says "the label says 'insufficient recent news' instead". A paying agent cannot tell which to expect. The real behavior on /sentiment is HTTP 422: `app/sentiment_service.py` line 74 raises it with "Not charged" in the message. That comes from reading the code. It was not seen on a real paid call.
- Exact change: make every place say 422. Remove "insufficient recent news" from `info.x-guidance`, the 200 description and the label `enum`, and reword the two lines in `app/home.py`. The rewritten description already says this. Keep the label in the /rapidapi documentation: that route returns it on purpose, because RapidAPI meters 4xx responses.
- Snippet:

```text
Remove from info.x-guidance:
"with too little recent news the label is \"insufficient recent news\" instead."
Replace with:
"with too little recent news the request is refused with HTTP 422 and not charged."

In app/home.py line 201, replace:
Low-coverage coins often get &ldquo;insufficient recent news&rdquo; (not charged).
With:
Low-coverage coins are often refused with HTTP 422 (not charged).

In app/home.py lines 245 and 246, replace:
When there's too little recent news to call it, the label says &ldquo;insufficient recent news&rdquo; instead.
With:
When there's too little recent news to call it, the request is refused with HTTP 422 and not charged.
```

### 5. Tidy the smaller mismatches

- About how long: about 20 minutes
- Evidence and exact change:
  - The example output in both routes' 402 shows `"window": "72h-hl24"`, but llms.txt says the current method is `72h-hl24-v2`. Update the examples.
  - /.well-known/x402 says `"updated": "2026-09-18T00:00:00Z"`. Update it whenever the routes change.
  - The root page `/` lists only `/sentiment` as the paid endpoint. Add `/history`.
  - The two paid routes have no `tags` in OpenAPI, and x402scan's sample includes them. Add one, for example `"tags": ["Sentiment"]`.
  - /history's `symbol` has no list of allowed values in OpenAPI, while /sentiment's does. Add one if the list is the same.
- Snippet:

```json
"tags": ["Sentiment"]
```

### 6. Make one paid call on each route, last

- About how long: about 5 minutes
- Evidence: the live /history description is 477 characters and the listing still has an older 384-character version. The missing sentence tells buyers that a bad request is not charged. The listing was last updated at the last paid call, on 2026-10-18. A listing only refreshes when a payment settles, so fixes 1 and 2 will not appear until then.
- Exact change: after fixes 1 to 5 are live, make one real call to /sentiment/BTC ($0.01) and one to /history/BTC ($0.05). Doing this first would waste the payment.
- Snippet:

```text
GET https://crypto-sentiment-x402.onrender.com/sentiment/BTC   $0.01
GET https://crypto-sentiment-x402.onrender.com/history/BTC     $0.05
```

## Description review

### GET /sentiment/:symbol

Current (486 characters):

> Real-time crypto sentiment for a ticker symbol (e.g. BTC, ETH, SOL) from 9 crypto news RSS outlets (CoinDesk, Cointelegraph, Decrypt, Bitcoin Magazine, The Block, CryptoSlate, NewsBTC, CryptoPotato, The Defiant) and the Fear & Greed Index. Headlines from the last 72h, weighted toward the newest. Returns a bullish/bearish/neutral label (or 'insufficient recent news'), score, sample size and the top 5 headlines behind it (title, link) as JSON. Path param: symbol, e.g. /sentiment/BTC.

Rewritten (449 characters, limit 500):

> Crypto news sentiment for one coin (24 tickers, e.g. BTC, ETH, SOL): a bullish, bearish or neutral label, a -1 to 1 score, sample size and the top 5 headlines behind it, with links. Built from 9 crypto news outlets and the Fear & Greed Index, last 72 hours, newest weighted most. Use when an agent needs a current read on news sentiment for a coin before research or a trade. Too little recent news or an unsupported coin is refused and not charged.

Opens with (search words): Crypto news sentiment

Use-when line: Use when an agent needs a current read on news sentiment for a coin before research or a trade.

Best 5 tags, most important first: crypto sentiment, bitcoin sentiment, news sentiment, fear and greed, crypto news

### GET /history/:symbol

Current (477 characters):

> Hourly crypto news-sentiment readings for a ticker (e.g. BTC, ETH), recorded live at the top of every UTC hour and never backfilled. Each row: observed_at, average_compound (-1 to 1), sample_size, Fear & Greed value, matcher and the top 3 headlines behind it (title, link). Query start/end as ISO 8601; default last 7 days, max 30 days per call. An unlogged symbol, an invalid range or a range with no readings is refused and not charged. See /archive for symbols and coverage.

Rewritten (413 characters, limit 500):

> Hourly crypto news sentiment history for one coin: score (-1 to 1), sample size, Fear & Greed value and top 3 headlines per hour, recorded live and never backfilled. Use when backtesting or comparing sentiment over time, up to 30 days per call (default 7). Query start/end as ISO 8601. A range with no readings, an invalid range or an unlogged coin is refused and not charged. See /archive for coins and coverage.

Opens with (search words): Hourly crypto news sentiment history

Use-when line: Use when backtesting or comparing sentiment over time, up to 30 days per call (default 7).

Best 5 tags, most important first: sentiment history, crypto sentiment, bitcoin sentiment, news sentiment, backtesting

Instructions-to-agents wording: none found in the current or rewritten text, using Unlisted's own small word list. That list is not CDP's screening, which is not public.

## Trust signals

Read at: 2026-11-02 19:23:10 to 19:23:19 UTC

| Signal | Result | Reading |
| --- | --- | --- |
| Health endpoint | GET /health returned 200 `{"status":"ok"}` in 0.24 s (median of 5) | Good |
| Response time, unpaid request | /sentiment/BTC median 0.32 s, slowest 0.47 s. /history/BTC median 0.28 s, slowest 0.50 s (5 reads each) | Good. No slow first request was seen. |
| Unknown path | GET /definitely-not-a-route-xyz returned 404 | Good |
| Prices | $0.01 and $0.05, in USDC on Base. The 39 distinct listings from the three searches charge $0.001 to $0.05, median $0.005. | Ordinary. /sentiment is about twice that median, and /history is at the top of the range, though that range is not limited to hourly archives. |

The price is the same in the 402, OpenAPI, llms.txt and both catalogs. The pay-to address passes its checksum and matches the listing.

## Evidence

What the catalog holds next to what the service says right now. A difference means the listing is out of date, unless the row says otherwise.

### GET /sentiment/:symbol

| Field | Bazaar listing, read 19:19:47 UTC | Live 402, read 19:18:19 UTC | Same? |
| --- | --- | --- | --- |
| Price | 10000 ($0.01) | 10000 ($0.01) | Yes |
| Pay-to address | 0xA6E10884a70D00DE8b8c5dfF0a2a9B1a821cE074 | 0xA6E10884a70D00DE8b8c5dfF0a2a9B1a821cE074 | Yes |
| Network and asset | Base (eip155:8453), USDC | Base (eip155:8453), USDC | Yes |
| Description | 486 characters | 486 characters | Yes, word for word |
| Tags | None | None | Yes, and that is the problem |
| Output schema | Present | Present | Yes |
| Allowed methods in schema | GET | GET, HEAD, DELETE | No. Set by the x402 library, nothing to do. |

### GET /history/:symbol

| Field | Bazaar listing, read 19:19:47 UTC | Live 402, read 19:18:20 UTC | Same? |
| --- | --- | --- | --- |
| Price | 50000 ($0.05) | 50000 ($0.05) | Yes |
| Pay-to address | 0xA6E10884a70D00DE8b8c5dfF0a2a9B1a821cE074 | 0xA6E10884a70D00DE8b8c5dfF0a2a9B1a821cE074 | Yes |
| Network and asset | Base (eip155:8453), USDC | Base (eip155:8453), USDC | Yes |
| Description | 384 characters, last updated 2026-10-18 | 477 characters | No. The listing lacks: "An unlogged symbol, an invalid range or a range with no readings is refused and not charged." |
| Tags | None | None | Yes, and that is the problem |
| Output schema | Present | Present | Yes |

Automated checks, run on both routes at 19:19:26 and 19:19:27 UTC: 13 of the 15 can run without a payment. 11 passed and none failed. The other 2 only report information: no tags declared, and the search rank for each route's own opening words. The OpenAPI file also has what catalogs ask for apart from fix 3: `x-payment-info` objects with price and protocols on both paid routes, a 402 response, input parameters, `info.x-guidance`, and `security: []` on every free route. /.well-known/x402 is present and lists both routes.

## Next steps and recheck

1. Make fixes 1 to 5 and deploy them. The 402 changes on purpose here, so compare it against the rewritten text rather than the old one.
2. Make the two paid calls in fix 6.
3. If x402scan does not show the service, register it at https://www.x402scan.com/resources/register. That needs a wallet sign-in, so the owner has to do it.

Recheck: read the listing the same day the paid calls settle, then run the three searches again one week later. It counts as fixed when both listings show the new description and 5 tags, and at least one route comes back for "crypto sentiment". Search ranking belongs to CDP, so better wording can improve the odds but cannot promise a position.

Not checked:

- x402scan: its data API charges $0.01 to $0.02 per read, so it was not called. The public page returned the same empty shell for this service and for a made-up address, because the page fills in inside a browser. The site map lists servers by ID and has 1,000 entries, so it cannot answer this either.
- Whether paid calls work: 2 of the 15 automated checks, `settlement_echo` and `extension_response`, need a real payment and were not run.
- The call counts include any test payments by the owner. The catalogs do not separate them.
- What happens after payment for a bad request. Unpaid, /sentiment/NOTACOIN and /history/BTC?start=garbage both return the normal 402, and the "refused and not charged" claim comes from the service's documentation.
- How Agentic.market decides which cards are enriched. 74 of 4,160 cards were enriched at 19:25 UTC, and its documentation does not say how to get there. The card's route text comes from the Bazaar listing, so fix 6 should update that part.
- One set of timings is a snapshot, not a measure of uptime.
