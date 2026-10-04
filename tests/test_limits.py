"""
Tests for limits.py: rate limiter, economic ceiling, and submission
logging. Uses an injectable fake clock throughout instead of real sleeps,
so window-expiry behavior is tested deterministically and instantly.
"""

import json
import logging
import threading

import pytest

from limits import (
    EconomicCeiling,
    RateLimitExceeded,
    SlidingWindowRateLimiter,
    log_submission,
)


class FakeClock:
    """A controllable clock for testing time-window behavior without
    sleeping. Starts at an arbitrary nonzero offset to catch bugs that
    assume time starts at 0."""

    def __init__(self, start: float = 1_000_000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


# --------------------------------------------------------------------------
# SlidingWindowRateLimiter
# --------------------------------------------------------------------------


def test_allows_up_to_the_limit():
    clock = FakeClock()
    limiter = SlidingWindowRateLimiter(limit=3, window_seconds=60, clock=clock)
    for _ in range(3):
        limiter.check_and_record("key")  # should not raise
    assert limiter.current_count("key") == 3


def test_blocks_once_over_the_limit():
    clock = FakeClock()
    limiter = SlidingWindowRateLimiter(limit=2, window_seconds=60, clock=clock)
    limiter.check_and_record("key")
    limiter.check_and_record("key")
    with pytest.raises(RateLimitExceeded):
        limiter.check_and_record("key")


def test_rate_limit_exceeded_carries_scope_and_key():
    clock = FakeClock()
    limiter = SlidingWindowRateLimiter(limit=1, window_seconds=60, clock=clock)
    limiter.check_and_record("1.2.3.4", scope="caller")
    with pytest.raises(RateLimitExceeded) as exc_info:
        limiter.check_and_record("1.2.3.4", scope="caller")
    assert exc_info.value.scope == "caller"
    assert exc_info.value.key == "1.2.3.4"
    assert exc_info.value.limit == 1


def test_keys_are_independent():
    clock = FakeClock()
    limiter = SlidingWindowRateLimiter(limit=1, window_seconds=60, clock=clock)
    limiter.check_and_record("a")
    limiter.check_and_record("b")  # different key, independent budget
    with pytest.raises(RateLimitExceeded):
        limiter.check_and_record("a")


def test_window_expiry_frees_up_budget():
    clock = FakeClock()
    limiter = SlidingWindowRateLimiter(limit=1, window_seconds=60, clock=clock)
    limiter.check_and_record("key")
    with pytest.raises(RateLimitExceeded):
        limiter.check_and_record("key")

    clock.advance(61)  # past the window
    limiter.check_and_record("key")  # should succeed now
    assert limiter.current_count("key") == 1


def test_sliding_window_is_not_a_fixed_bucket():
    clock = FakeClock()
    limiter = SlidingWindowRateLimiter(limit=2, window_seconds=60, clock=clock)
    limiter.check_and_record("key")  # t=0
    clock.advance(30)
    limiter.check_and_record("key")  # t=30, still within window of the first
    with pytest.raises(RateLimitExceeded):
        limiter.check_and_record("key")  # t=30, both prior hits still active

    clock.advance(31)  # t=61: first hit (t=0) has aged out, second (t=30) hasn't
    limiter.check_and_record("key")  # should succeed -- only 1 active hit
    assert limiter.current_count("key") == 2


def test_concurrent_calls_never_exceed_the_limit():
    # Real time.monotonic() here on purpose -- this is specifically testing
    # the lock under actual thread concurrency, not window logic.
    limiter = SlidingWindowRateLimiter(limit=10, window_seconds=60)
    accepted = []
    lock = threading.Lock()

    def worker():
        try:
            limiter.check_and_record("shared-key")
            with lock:
                accepted.append(1)
        except RateLimitExceeded:
            pass

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(accepted) == 10


# --------------------------------------------------------------------------
# EconomicCeiling
# --------------------------------------------------------------------------


def test_price_within_cap():
    ceiling = EconomicCeiling(per_request_cap=0.05, global_ceiling=2.00)
    assert ceiling.price_within_cap(0.05) is True
    assert ceiling.price_within_cap(0.049) is True
    assert ceiling.price_within_cap(0.051) is False


def test_can_spend_within_global_ceiling():
    clock = FakeClock()
    ceiling = EconomicCeiling(per_request_cap=0.05, global_ceiling=1.00, clock=clock)
    assert ceiling.can_spend(0.5) is True
    ceiling.record_spend(0.5)
    assert ceiling.can_spend(0.5) is True  # exactly at the ceiling
    ceiling.record_spend(0.5)
    assert ceiling.can_spend(0.01) is False  # would exceed it


def test_record_spend_without_can_spend_check_still_accumulates():
    clock = FakeClock()
    ceiling = EconomicCeiling(per_request_cap=0.05, global_ceiling=1.00, clock=clock)
    ceiling.record_spend(0.3)
    ceiling.record_spend(0.3)
    assert ceiling.current_spend() == pytest.approx(0.6)


def test_spend_window_expiry():
    clock = FakeClock()
    ceiling = EconomicCeiling(
        per_request_cap=0.05, global_ceiling=1.00, window_seconds=86400, clock=clock
    )
    ceiling.record_spend(0.99)
    assert ceiling.can_spend(0.02) is False

    clock.advance(86401)  # past the rolling window
    assert ceiling.current_spend() == 0.0
    assert ceiling.can_spend(0.99) is True


def test_can_spend_does_not_record():
    clock = FakeClock()
    ceiling = EconomicCeiling(per_request_cap=0.05, global_ceiling=1.00, clock=clock)
    ceiling.can_spend(0.5)
    ceiling.can_spend(0.5)
    assert ceiling.current_spend() == 0.0  # can_spend alone never accumulates


# --------------------------------------------------------------------------
# log_submission
# --------------------------------------------------------------------------


def test_log_submission_logs_structured_json(caplog):
    with caplog.at_level(logging.INFO, logger="x402_doctor"):
        log_submission(
            "https://api.example.com/data", blocked=False, caller="1.2.3.4"
        )

    assert len(caplog.records) == 1
    record = json.loads(caplog.records[0].message)
    assert record["event"] == "diagnose_submission"
    assert record["url"] == "https://api.example.com/data"
    assert record["domain"] == "api.example.com"
    assert record["blocked"] is False
    assert record["caller"] == "1.2.3.4"


def test_log_submission_records_blocked_requests_too(caplog):
    """Per the spec: blocked submissions must be logged too, not just
    successful checks -- that's the whole point of this function existing
    separately from just logging successes."""
    with caplog.at_level(logging.INFO, logger="x402_doctor"):
        log_submission(
            "http://169.254.169.254/secret",
            blocked=True,
            reason="ssrf_blocked",
            caller="9.9.9.9",
        )

    record = json.loads(caplog.records[0].message)
    assert record["blocked"] is True
    assert record["reason"] == "ssrf_blocked"


def test_log_submission_handles_unparseable_url_gracefully(caplog):
    with caplog.at_level(logging.INFO, logger="x402_doctor"):
        log_submission("not a url at all", blocked=True, reason="invalid")
    record = json.loads(caplog.records[0].message)
    assert record["url"] == "not a url at all"


def test_log_submission_drops_query_string(caplog):
    with caplog.at_level(logging.INFO, logger="x402_doctor"):
        log_submission(
            "https://api.example.com/data?api_key=secret123&x=1", blocked=False
        )
    record = json.loads(caplog.records[0].message)
    assert record["url"] == "https://api.example.com/data"
    assert record["domain"] == "api.example.com"
    assert "secret123" not in caplog.records[0].message


def test_release_gives_back_the_most_recent_hit():
    from limits import RateLimitExceeded, SlidingWindowRateLimiter

    limiter = SlidingWindowRateLimiter(limit=1, window_seconds=60)
    limiter.check_and_record("a.test")
    limiter.release("a.test")
    assert limiter.current_count("a.test") == 0
    limiter.check_and_record("a.test")  # not blocked
    limiter.release("b.test")  # no hits: a no-op
    with pytest.raises(RateLimitExceeded):
        limiter.check_and_record("a.test")
