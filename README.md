# Unlisted

Unlisted finds out why an x402 endpoint isn't in the CDP Bazaar.

- Website: https://unlisted.sh
- Free guide: https://unlisted.sh/guide
- API docs: https://unlisted.sh/docs
- For AI agents: https://unlisted.sh/llms.txt

## Modes and prices

Pay per call in USDC on Base through x402. No signup and no API key.

| Mode | Price | What it does |
| --- | --- | --- |
| Check | $0.02 | Reads your 402 challenge and Bazaar declaration, and asks CDP for its live index status. |
| Check + real payment (`?mode=paid`) | $0.10 | Does the same, then makes a real test payment for routes priced up to $0.05 so CDP has a settlement to index. Once per target domain per 24 hours. |

Unlisted can't make CDP index a route, so neither mode promises a listing.

## When you're charged

In both modes, an unpaid request returns HTTP 402 and costs nothing. A paid request is charged only if it returns HTTP 200. A 4xx or 5xx response isn't charged, for example 400 for an example.com URL, 429 for a rate limit or 502 if the target can't be fetched.

- **Check:** you're charged when the check completes. That includes a target that answers with something other than a 402, because you still get a report (HTTP 200).
- **Check + real payment:** you're charged only when a test payment is attempted, meaning a payment-carrying request is sent to your target, whatever its outcome. If no test payment is made, the response is HTTP 422, you aren't charged, and the domain's once-per-24-hours test isn't used up. This happens when the target answers with something other than a 402, or with a 402 Unlisted can't pay, such as one priced above $0.05.

## Call it

```http
POST https://unlisted.sh/diagnose
Content-Type: application/json

{"url": "https://your-api.example.com/paid-route"}
```

**The URL above is a placeholder. Replace it with your own x402 endpoint: the service rejects example.com URLs.**

An unpaid request returns HTTP 402 with the payment requirements. Pay with x402 and retry the same request. For a real test payment, send it to `https://unlisted.sh/diagnose?mode=paid`.

## Contact

hi@unlisted.sh

Unlisted isn't affiliated with Coinbase.
