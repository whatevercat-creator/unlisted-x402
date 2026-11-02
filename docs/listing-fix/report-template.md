# Listing Fix report: {{service name}}

Service: {{origin URL}}
Client stack: {{Python / Node / Go}}. Every snippet in this report must match this stack.
Paid routes checked: {{METHOD /route ($price), ...}}
Report written: {{YYYY-MM-DD HH:MM UTC}}

How this was checked: read-only. No payments, no signing, no browser, and no access to the seller's code. Anything that could not be checked that way is marked "not checked" with the reason.

## Summary

{{Line 1: can buyers find this service today?}}
{{Line 2: the main reason, in one sentence.}}
{{Line 3: the first thing to do, and what it costs.}}

## Do this

- [ ] Fix 1: {{short name}}
- [ ] {{One line for each fix, in the same order as "Fixes in order". Leave out the paid call fix here.}}
- [ ] Deploy the fixes and check they are live.
- [ ] Last, make the paid calls: {{routes, and total cost}}.

## Catalog status

| Catalog | Read at (UTC) | Listed? | What buyers see | Problem |
| --- | --- | --- | --- | --- |
| CDP Bazaar | {{time}} | {{yes/no, which routes}} | {{last called, 30-day calls and payers}} | {{problem or "none"}} |
| Bazaar search | {{time}} | {{rank for each phrase}} | {{what ranks instead}} | {{problem or "none"}} |
| x402scan | {{time}} | {{yes/no/not checked}} | {{routes shown}} | {{problem, or why it could not be checked}} |
| Agentic.market | {{time}} | {{card yes/no, enriched yes/no}} | {{what the card shows}} | {{problem or "none"}} |

## Fixes in order

Do these from top to bottom. Each fix says what was seen, what to change and what the change looks like. Say plainly how sure the evidence is: "the catalog shows this" is stronger than "this might help".

### 1. {{Short name of the fix}}

- About how long: {{e.g. about 10 minutes}}
- Evidence: {{what was seen, and when}}
- Exact change: {{what to change, and where}}
- Snippet:

```text
{{the code or config to use}}
```

{{Repeat for each fix. The last fix is usually the paid call that refreshes the listing.}}

## Description review

### {{METHOD /route}}

Current ({{n}} characters):

> {{live description}}

Rewritten ({{n}} characters, limit 500):

> {{new description}}

Use-when line: {{the "Use when ..." sentence}}

Best 5 tags, most important first: {{tag 1, tag 2, tag 3, tag 4, tag 5}}

{{Repeat for each route.}}

## Trust signals

Read at: {{time}}

| Signal | Result | Reading |
| --- | --- | --- |
| Health endpoint | {{path, status}} | {{good / problem}} |
| Response time, unpaid request | {{median of n, slowest}} | {{good / problem}} |
| Unknown path | {{status}} | {{should be 404}} |
| Prices | {{route prices, what similar listings charge}} | {{ordinary / unusual}} |

## Evidence

What the catalog holds next to what the service says right now. A difference means the listing is out of date, unless the row says otherwise.

If a row doesn't match but needs no action, say why in the "Same?" column in a few words, e.g. "No. Set by the x402 library, nothing to do."

### {{METHOD /route}}

| Field | Bazaar listing, read {{time}} UTC | Live 402, read {{time}} UTC | Same? |
| --- | --- | --- | --- |
| Price | {{}} | {{}} | {{}} |
| Pay-to address | {{}} | {{}} | {{}} |
| Network and asset | {{}} | {{}} | {{}} |
| Description | {{}} | {{}} | {{}} |
| Tags | {{}} | {{}} | {{}} |
| Output schema | {{}} | {{}} | {{}} |

{{Repeat for each route.}}

## Next steps and recheck

1. {{Who does what, in order.}}

Recheck: {{when, and exactly what to read again, and what result counts as fixed.}}

Not checked: {{everything this report could not confirm, and why.}}
