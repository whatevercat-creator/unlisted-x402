"""
Rate limiting, economic ceiling, and submission logging for x402 Doctor.

In-process only, by explicit decision: no Redis, no external store. That
means these limits reset on process restart and don't share state across
multiple worker processes -- real limitations once this runs behind more
than one uvicorn worker, but nothing to provision to get this far, and the
public interface here (check_and_record / can_spend / record_spend) is
what a Redis-backed swap-in would need to preserve, not something callers
should need to know about.

Per the spec's safety section, none of this is optional hardening to add
"later" -- it's what stands between "someone finds the endpoint" and the
outbound test-payment wallet (once that exists) draining faster than the
$0.02 fee recoups it, or this service being used to hammer someone else's
real endpoint under cover of "testing" it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
import logging
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, Optional
from urllib.parse import urlparse

logger = logging.getLogger("x402_doctor")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------


class RateLimitExceeded(Exception):
    def __init__(self, scope: str, key: str, limit: int, window_seconds: float):
        self.scope = scope
        self.key = key
        self.limit = limit
        self.window_seconds = window_seconds
        super().__init__(
            f"{scope} rate limit exceeded for {key!r}: {limit} per {window_seconds:.0f}s"
        )


class SlidingWindowRateLimiter:
    """A basic in-process sliding-window counter, keyed by an arbitrary
    string (a caller IP, a target domain, eventually a payer wallet
    address). Thread-safe -- FastAPI/uvicorn can run requests concurrently
    even within a single process, so a bare dict-of-deques without a lock
    would let concurrent requests race past the limit.

    `clock` is injectable so tests can advance time deterministically
    instead of sleeping; defaults to time.monotonic for real use.
    """

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.limit = limit
        self.window_seconds = window_seconds
        self._clock = clock
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str) -> Deque[float]:
        hits = self._hits[key]
        cutoff = self._clock() - self.window_seconds
        while hits and hits[0] < cutoff:
            hits.popleft()
        return hits

    def check_and_record(self, key: str, scope: str = "rate_limit") -> None:
        """Raises RateLimitExceeded if `key` is already at its limit within
        the window; otherwise records this call and returns."""
        with self._lock:
            hits = self._prune(key)
            if len(hits) >= self.limit:
                raise RateLimitExceeded(scope, key, self.limit, self.window_seconds)
            hits.append(self._clock())

    def current_count(self, key: str) -> int:
        with self._lock:
            return len(self._prune(key))


# --------------------------------------------------------------------------
# Economic ceiling
# --------------------------------------------------------------------------


@dataclass
class _SpendRecord:
    amount: float
    at: float


class EconomicCeiling:
    """Two layers per the spec, tracked independently:

    1. Per-request price cap (`price_within_cap`) -- only proceed with a
       real test payment if the target's advertised price is at or below a
       small ceiling; above that, the caller should fall back to
       inspection-only rather than asking this class anything.
    2. Rolling-window global spend ceiling (`can_spend` / `record_spend`)
       -- the actual backstop against a determined attacker standing up
       many cheap endpoints (or reusing one from many wallets) to drain the
       test-payment wallet faster than fees recoup it. This is independent
       of any single request's price.

    Not yet wired into anything that spends money -- dry-check never makes
    a real payment, so there's nothing for this to gate yet. It's built and
    tested now so the payment module (build-order steps 6-7) has it ready
    rather than needing to retrofit safety rails after the fact.
    """

    def __init__(
        self,
        per_request_cap: float,
        global_ceiling: float,
        window_seconds: float = 86400,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.per_request_cap = per_request_cap
        self.global_ceiling = global_ceiling
        self.window_seconds = window_seconds
        self._clock = clock
        self._records: Deque[_SpendRecord] = deque()
        self._lock = threading.Lock()

    def price_within_cap(self, price: float) -> bool:
        return price <= self.per_request_cap

    def _current_spend_locked(self) -> float:
        cutoff = self._clock() - self.window_seconds
        while self._records and self._records[0].at < cutoff:
            self._records.popleft()
        return sum(r.amount for r in self._records)

    def current_spend(self) -> float:
        with self._lock:
            return self._current_spend_locked()

    def can_spend(self, amount: float) -> bool:
        """True if spending `amount` now would stay within the rolling
        global ceiling. Does not record the spend -- call `record_spend`
        separately, and only after the payment actually goes through, so a
        payment that fails partway through never gets counted."""
        with self._lock:
            return self._current_spend_locked() + amount <= self.global_ceiling

    def record_spend(self, amount: float) -> None:
        with self._lock:
            self._records.append(_SpendRecord(amount=amount, at=self._clock()))


# --------------------------------------------------------------------------
# Submission logging
# --------------------------------------------------------------------------


def log_submission(
    url: str,
    *,
    blocked: bool,
    reason: Optional[str] = None,
    caller: Optional[str] = None,
) -> None:
    """Log every submitted target URL, including ones blocked by
    validation -- not just successful checks. Per the spec: a pattern of
    systematic internal-IP or sequential-IP submissions is a signal worth
    having visibility into even though each individual request was
    correctly blocked. One JSON object per line (stdout, for a platform
    like Render to capture) rather than a formatted message, so this is
    grep/jq-able later without needing a real log pipeline yet.
    """
    try:
        domain = urlparse(url).hostname
    except ValueError:
        domain = None

    record = {
        "event": "diagnose_submission",
        "url": url,
        "domain": domain,
        "blocked": blocked,
        "reason": reason,
        "caller": caller,
    }
    logger.info(json.dumps(record))


def log_usage(
    *,
    url: str,
    method: str,
    mode: str,
    paywall_active: bool,
    payer: Optional[str],
    report: Any,
    bazaar_summary: dict,
    duration_ms: int,
) -> None:
    """One JSON line per *completed* diagnosis -- the usage record: who
    used Unlisted, on what, and what they got back. Paired with
    log_submission (every attempt, incl. blocked ones). grep Render's logs
    for "diagnose_usage" to count real usage."""
    checks = getattr(report, "checks", []) or []
    counts: dict[str, int] = {}
    settlement = None
    for c in checks:
        status = getattr(c.status, "value", str(c.status))
        counts[status] = counts.get(status, 0) + 1
        if c.check_id == "settlement_echo":
            settlement = status
    try:
        parsed = urlparse(url)
        domain = parsed.hostname
        # Query strings can carry the target's API keys -- never log them.
        logged_url = parsed._replace(query="").geturl()
    except ValueError:
        domain = None
        logged_url = url.split("?", 1)[0]
    record = {
        "event": "diagnose_usage",
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "domain": domain,
        "url": logged_url,
        "method": method,
        "mode": mode,
        "paywall_active": paywall_active,
        "payer": payer,
        "target_http_status": getattr(report, "http_status", None),
        "bazaar": bazaar_summary.get("status"),
        "checks": counts,
        "settlement": settlement,
        "duration_ms": duration_ms,
    }
    logger.info(json.dumps(record))
