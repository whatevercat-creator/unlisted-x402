"""
extensions_placement (guide cause 4) and the non-402 verdicts (guide cause
13): a Bazaar block inside accepts[0], and a submitted URL that answers
with something other than a 402.
"""

import httpx
import pytest

import dry_check
import main
import paid_check
import safe_fetch as sf
from diagnosis import (
    Status,
    check_bazaar_extension,
    check_extensions_placement,
    extract_accepts,
    non_402_verdict,
    run_checks,
    summarize,
)
from dry_check import run_dry_check
from paid_check import run_paid_check
from tests.test_diagnosis import _good_challenge
from tests.test_dry_check import _streaming_response, fake_resolver  # noqa: F401
from tests.test_paid_check import SIGNER, make_ceiling
from tests.test_payment import (  # noqa: F401
    _build_paid_client,
    _decode_challenge,
    _payment_signature_header_for,
    reset_limiters,
)


def _nested_challenge(bazaar=None):
    challenge = _good_challenge()
    block = challenge.pop("extensions")
    if bazaar is not None:
        block = {"bazaar": bazaar}
    challenge["accepts"][0]["extensions"] = block
    return challenge


def _by_id(checks):
    return {c.check_id: c for c in checks}


# --------------------------------------------------------------------------
# extensions_placement
# --------------------------------------------------------------------------


def test_placement_passes_at_top_level():
    challenge = _good_challenge()
    result = check_extensions_placement(challenge, extract_accepts(challenge))
    assert result.status == Status.PASS
    assert "next to `accepts`" in result.detail


def test_placement_fails_when_only_inside_accepts_and_contents_still_checked():
    checks = _by_id(run_checks(_nested_challenge(), "https://api.example.com/data"))
    placement = checks["extensions_placement"]
    assert placement.status == Status.FAIL
    assert "inside accepts[0]" in placement.detail
    assert "Move `extensions` out of accepts[0] to the top level of the 402 body, next to `accepts`" in placement.fix
    # The nested block is otherwise fine, and the report says so.
    assert checks["bazaar_extension"].status == Status.PASS
    assert "inside accepts[0]" in checks["bazaar_extension"].detail
    # routeTemplate inside the nested block is still read.
    assert checks["route_template"].status == Status.PASS


def test_nested_block_no_longer_reads_as_no_issues():
    verdict = summarize(run_checks(_nested_challenge(), "https://api.example.com/data"))
    assert "No issues found" not in verdict
    assert "extensions_placement" in verdict


def test_nested_malformed_block_fails_both_checks():
    checks = _by_id(run_checks(_nested_challenge({"info": {"input": {}}}), "https://api.example.com/data"))
    assert checks["extensions_placement"].status == Status.FAIL
    assert checks["bazaar_extension"].status == Status.FAIL
    assert "input.type" in checks["bazaar_extension"].detail


def test_nested_bazaar_found_when_top_level_extensions_lacks_it():
    challenge = _nested_challenge()
    challenge["extensions"] = {"other": {}}
    accepts = extract_accepts(challenge)
    assert check_extensions_placement(challenge, accepts).status == Status.FAIL
    assert check_bazaar_extension(challenge, accepts).status == Status.PASS


def test_top_level_wins_when_both_present():
    challenge = _good_challenge()
    challenge["accepts"][0]["extensions"] = {"bazaar": {"info": {}}}
    accepts = extract_accepts(challenge)
    assert check_extensions_placement(challenge, accepts).status == Status.PASS
    assert check_bazaar_extension(challenge, accepts).status == Status.PASS


def test_placement_skips_without_any_bazaar_block():
    challenge = _good_challenge()
    del challenge["extensions"]
    result = check_extensions_placement(challenge, extract_accepts(challenge))
    assert result.status == Status.SKIP


def test_clean_challenge_still_reads_no_issues():
    assert "No issues found" in summarize(run_checks(_good_challenge(), "https://api.example.com/data"))


# --------------------------------------------------------------------------
# non_402_verdict
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 409, 422])
def test_validation_statuses_name_validation_before_paywall(status):
    verdict = non_402_verdict(status)
    assert verdict.startswith(f"Expected HTTP 402, got {status}.")
    assert "validation is most likely running before the paywall" in verdict
    assert "never see the price" in verdict
    assert "required parameters" in verdict and "`method`" in verdict and "`body`" in verdict
    assert "x402 middleware answers an unpaid request with a 402 before validation runs" in verdict


@pytest.mark.parametrize("status", [401, 403])
def test_auth_statuses(status):
    assert "some other authentication" in non_402_verdict(status)


def test_404_names_the_path():
    assert "Nothing is served at this URL for POST" in non_402_verdict(404, method="POST")


def test_405_suggests_the_other_method():
    get = non_402_verdict(405, method="GET", allow="POST")
    assert "doesn't accept GET (its Allow header says: POST)" in get
    assert "`method` set to POST" in get
    assert "`method` set to GET" in non_402_verdict(405, method="POST")


@pytest.mark.parametrize("status", [500, 502, 503])
def test_5xx(status):
    assert "server errored before it could answer with a 402" in non_402_verdict(status)


def test_200_and_unlisted_statuses_keep_the_old_text():
    assert "may not require payment at all" in non_402_verdict(200)
    assert "Can't run the payment-requirement checks" in non_402_verdict(302)


def test_paid_mode_says_no_test_payment_was_made():
    verdict = non_402_verdict(400, mode="paid")
    assert "validation is most likely running before the paywall" in verdict
    assert verdict.endswith("No test payment was made: there's no 402 challenge to pay against.")
    assert "Can't run" not in non_402_verdict(302, mode="paid")


def test_verdicts_never_promise_a_listing():
    for status in (200, 302, 400, 401, 404, 405, 409, 422, 500):
        for mode in ("dry", "paid"):
            verdict = non_402_verdict(status, mode=mode).lower()
            assert "will be listed" not in verdict and "will be indexed" not in verdict
            assert "guarantee" not in verdict


# --------------------------------------------------------------------------
# End to end: dry, paid, and whether the caller is charged
# --------------------------------------------------------------------------


def _target_answering(status, headers=None):
    def handler(request: httpx.Request) -> httpx.Response:
        return _streaming_response(status, b'{"error": "missing symbol"}', headers=headers)

    return httpx.MockTransport(handler)


async def test_dry_check_400_gets_validation_verdict(fake_resolver, monkeypatch):  # noqa: F811
    transport = _target_answering(400)

    async def _patched(url, method="GET", **kwargs):
        return await sf.safe_fetch(url, method=method, transport=transport, **kwargs)

    monkeypatch.setattr(dry_check, "safe_fetch", _patched)
    report = await run_dry_check("https://api.seller.test/sentiment")
    assert report.http_status == 400
    assert report.checks == []
    assert "validation is most likely running before the paywall" in report.verdict


async def test_dry_check_405_quotes_allow_header(fake_resolver, monkeypatch):  # noqa: F811
    transport = _target_answering(405, headers={"allow": "POST"})

    async def _patched(url, method="GET", **kwargs):
        return await sf.safe_fetch(url, method=method, transport=transport, **kwargs)

    monkeypatch.setattr(dry_check, "safe_fetch", _patched)
    report = await run_dry_check("https://api.seller.test/analyze")
    assert "(its Allow header says: POST)" in report.verdict


async def test_paid_check_422_gets_validation_verdict_and_pays_nothing(fake_resolver):  # noqa: F811
    ceiling = make_ceiling()
    report = await run_paid_check(
        "https://api.seller.test/sentiment",
        signer=SIGNER,
        economic_ceiling=ceiling,
        transport=_target_answering(422),
    )
    assert report.mode == "paid"
    assert report.checks == []
    assert "validation is most likely running before the paywall" in report.verdict
    assert "No test payment was made" in report.verdict
    assert ceiling.current_spend() == 0


def test_caller_is_charged_for_a_completed_non_402_report(fake_resolver, monkeypatch):  # noqa: F811
    """Unchanged behaviour: a non-402 target still yields a completed report
    (HTTP 200), and the paywall settles every response under 400."""
    client, fake = _build_paid_client()
    challenge = _decode_challenge(client.post("/diagnose", json={"url": "https://api.seller.test/x"}))
    transport = _target_answering(400)

    async def _patched(url, method="GET", **kwargs):
        return await sf.safe_fetch(url, method=method, transport=transport, **kwargs)

    monkeypatch.setattr(dry_check, "safe_fetch", _patched)
    resp = client.post(
        "/diagnose",
        json={"url": "https://api.seller.test/x"},
        headers={"PAYMENT-SIGNATURE": _payment_signature_header_for(challenge)},
    )
    assert resp.status_code == 200
    assert resp.json()["http_status"] == 400
    assert "validation is most likely running before the paywall" in resp.json()["verdict"]
    assert len(fake.settle_calls) == 1
