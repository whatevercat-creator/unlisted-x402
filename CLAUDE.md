# Unlisted (repo unlisted-x402)

Unlisted finds out why an x402 endpoint isn't in the Coinbase CDP Bazaar.
It's a paid x402 API (POST /diagnose: dry Check, or `?mode=paid` with one
real test payment), plus a free guide (/guide) and the Listing Fix offer
(/fix, a written report the seller applies). Live at https://unlisted.sh.

- Branch: `master`. Render service srv-daqqitt9fdbs73c0i3lg auto-deploys
  every push to master. Python version comes from `.python-version`.
- `x402==2.25.0` is pinned on purpose (2.26.0 changed the unpaid 402).
  Upgrading any pinned dependency is its own change with the full routine.
- Main files: `main.py` (app, routes), `payment.py` (paywall, prices, our
  own Bazaar metadata), `dry_check.py` / `paid_check.py` / `diagnosis.py` /
  `probe.py` / `bazaar.py` (the checks), `guide.py`, `fix_page.py`.

## Run the tests

No network and no payments: the facilitator is stubbed.

```sh
uv venv --python "$(cat .python-version)" ~/.venvs/unlisted
uv pip install --python ~/.venvs/unlisted/bin/python -r requirements-dev.txt
env -i HOME="$HOME" PATH=/usr/bin:/bin ~/.venvs/unlisted/bin/python -m pytest -q
```

Report passed/failed counts and any new warnings.

## House rules

1. Use the **x402-safe-deploy** skill for any change, merge or deploy:
   branch, tests, 402 check, push window, live verification.
2. The unpaid 402 for POST /diagnose stays **byte-identical** unless the
   change is planned and the before/after is shown field by field.
   Baselines go in `.baseline402/` (git-ignored).
3. Push only between **:05 and :50 past the hour, US Eastern** (each push
   redeploys production). `.claude/hooks/push_window.py` blocks `git push`
   outside that window.
4. Never write to the production database. Data fixes are scripts with a
   default dry run (counts and key prefixes only); Claudine runs them in
   the Render Shell and runs `--apply` herself after reviewing.
5. No paid calls (to Unlisted or anything else) without the
   **x402-confirm-before-pay** skill.
6. Never type a wallet or contract address from memory. Read payTo from a
   live 402 and check its EIP-55 checksum.
7. Never promise a listing ("get listed", "guaranteed", "will appear").
   The Bazaar and each catalog decide; Unlisted fixes what the checks find.
8. Never open client or prospect URLs in a browser.
9. Secrets only via Render env vars or a local `.env`. Never in chat, code,
   logs or commits. Don't print, copy or change them; Render env var
   changes are Claudine's to make.
10. Work on a branch, change only what was asked, and stop if any
    pre-push step fails or gives no result.

See `STATUS.md` for what's live and what's open.
