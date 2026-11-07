# Status

Last updated: 2026-11-06. Facts come from the repo unless marked.
"(unverified)" = not confirmed from the repo or a live read.

## What's live

- **master at the `step2-nov6` merge**, pushed 2026-11-06 (Eastern), on
  top of d52df0d ("Merge branch 'header-check'"). Find the hash with
  `git log --merges -1 --grep step2-nov6`. It added:
  - `next_step` line in /diagnose reports that have a failed check.
  - Planned 402 change: "Use when" description, tags
    `bazaar, x402, diagnostics, listing, cdp`, fuller input/output schema.
    `accepts[]` (price, payTo, network, asset) unchanged.
  - `CLAUDE.md`, this file, and the push-window hook.
- Unlisted's own Bazaar listing still shows the old description and tags
  until a new paid call settles (open item 1).

## Prices

| What | Price | Source |
| --- | --- | --- |
| Check (dry) | $0.02 | `payment.DEFAULT_PRICE_USD` (env-overridable); live 402 shows amount 20000 = $0.02 |
| Check + real payment | $0.10 | `payment.DEFAULT_PAID_PRICE_USD` (env-overridable; live value unverified) |
| Listing Fix, standard | $49 | `fix_page.PRICE_USD` |
| Listing Fix, founding | $29 | `fix_page.FOUNDING_PRICE_USD`, first `FOUNDING_CLIENTS` = 3 clients |

- `FOUNDING_OPEN = True`: the founding offer is shown on /fix.
- `STRIPE_LINKS_SET = True`: both Stripe links are set, so the buy buttons
  are live.
- How many founding spots are taken: not in the repo (unverified).
- `/fix` page dated `FIX_UPDATED_ISO = 2026-11-06`.

## Open items

1. **Refresh Unlisted's own listing** after `step2-nov6` deploys: one paid
   call, run by Claudine with x402-confirm-before-pay. The Bazaar only takes
   the new description and tags from a new settlement.
2. **Live index lookup**: the README says the Check "asks CDP for its live
   index status". That depends on `X402_DOCTOR_ENABLE_BAZAAR_LOOKUP` on
   Render (unverified). The new 402 description no longer claims it.
3. **Card statement name**: `fix_page.STATEMENT_NAME` is "CRYPTO SENTIMENT",
   with a code comment to confirm it in Stripe before deploy. Whether that
   was done: unverified.
4. **Documented error responses** (one of CDP's curation criteria): not
   checked for Unlisted's own route.

## Dates

- **Mon Nov 16**: search check. (From the step-2 brief; exactly what gets
  checked isn't recorded in the repo. Likely Unlisted's own Bazaar search
  rank, unverified.)
- **Thu Nov 19**: Listing Fix decision. (From the step-2 brief; the
  criteria aren't recorded in the repo.)
