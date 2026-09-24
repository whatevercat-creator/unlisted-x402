"""Unit tests for diagnosis.py's checks.

`_good_challenge()` matches the CONFIRMED real shape (from a live curl
against crypto-sentiment-x402.onrender.com on September 24, 2026):
`resource`, `description` (nested under resource), and `extensions` all
live at the challenge level, not per-accepts-entry. A second set of tests
covers the older per-accept fallback, since a different seller's
implementation might still use it.
"""

import base64
import json

import pytest

from diagnosis import (
    ChallengeParseError,
    Status,
    check_bazaar_extension,
    check_description_length,
    check_resource_present,
    check_route_template,
    check_scheme_mismatch,
    decode_challenge,
    extract_accepts,
    parse_challenge,
    run_checks,
    summarize,
)


def _good_challenge(**overrides):
    challenge = {
        "x402Version": 2,
        "error": "Payment required",
        "resource": {
            "url": "https://api.example.com/data",
            "description": "A perfectly reasonable description.",
            "mimeType": "application/json",
        },
        "accepts": [
            {
                "scheme": "exact",
                "network": "eip155:8453",
                "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                "amount": "10000",
                "payTo": "0xA6E10884a70D00DE8b8c5dfF0a2a9B1a821cE074",
                "maxTimeoutSeconds": 300,
                "extra": {"name": "USD Coin", "version": "2"},
            }
        ],
        "extensions": {
            "bazaar": {
                "info": {"input": {"type": "http", "method": "GET"}},
                "routeTemplate": "/data/:id",
            }
        },
    }
    challenge.update(overrides)
    return challenge


# --------------------------------------------------------------------------
# parse_challenge / decode_challenge / extract_accepts
# --------------------------------------------------------------------------


def test_parse_challenge_rejects_non_json():
    with pytest.raises(ChallengeParseError):
        parse_challenge(b"not json at all")


def test_parse_challenge_rejects_non_object_json():
    with pytest.raises(ChallengeParseError):
        parse_challenge(b"[1, 2, 3]")


def test_parse_challenge_accepts_object():
    data = parse_challenge(b'{"x402Version": 1, "accepts": []}')
    assert data["x402Version"] == 1


def test_decode_challenge_prefers_header_over_body():
    challenge = _good_challenge()
    header_value = base64.b64encode(json.dumps(challenge).encode()).decode()
    decoded = decode_challenge(header_value, b"{}")
    assert decoded["resource"]["url"] == "https://api.example.com/data"


def test_decode_challenge_falls_back_to_body_when_no_header():
    challenge = _good_challenge()
    decoded = decode_challenge(None, json.dumps(challenge).encode())
    assert decoded["resource"]["url"] == "https://api.example.com/data"


def test_decode_challenge_falls_back_to_body_on_garbage_header():
    challenge = _good_challenge()
    decoded = decode_challenge("not-valid-base64!!!", json.dumps(challenge).encode())
    assert decoded["resource"]["url"] == "https://api.example.com/data"


def test_decode_challenge_raises_when_both_header_and_body_fail():
    with pytest.raises(ChallengeParseError):
        decode_challenge("not-valid-base64!!!", b"also not json")


def test_extract_accepts_prefers_accepts_array():
    challenge = _good_challenge()
    result = extract_accepts(challenge)
    assert len(result) == 1
    assert result[0]["payTo"] == "0xA6E10884a70D00DE8b8c5dfF0a2a9B1a821cE074"


def test_extract_accepts_falls_back_to_whole_object():
    flat = {"scheme": "exact", "resource": "https://api.example.com/data"}
    assert extract_accepts(flat) == [flat]


# --------------------------------------------------------------------------
# check_resource_present -- challenge-level (confirmed shape)
# --------------------------------------------------------------------------


def test_resource_present_passes_challenge_level_object():
    challenge = _good_challenge()
    result = check_resource_present(challenge, extract_accepts(challenge))
    assert result.status == Status.PASS


def test_resource_present_fails_when_missing_everywhere():
    challenge = _good_challenge()
    del challenge["resource"]
    challenge["accepts"] = [{k: v for k, v in challenge["accepts"][0].items()}]
    result = check_resource_present(challenge, extract_accepts(challenge))
    assert result.status == Status.FAIL


def test_resource_present_falls_back_to_per_accept_legacy_shape():
    # older/hypothetical shape: resource lives on the accepts[] entry itself
    challenge = {"accepts": [{"resource": "https://api.example.com/legacy"}]}
    result = check_resource_present(challenge, extract_accepts(challenge))
    assert result.status == Status.PASS
    assert "legacy" in result.detail


# --------------------------------------------------------------------------
# check_scheme_mismatch
# --------------------------------------------------------------------------


def test_scheme_mismatch_passes_when_schemes_match():
    challenge = _good_challenge()
    challenge["resource"]["url"] = "https://api.example.com/data"
    result = check_scheme_mismatch(
        challenge, extract_accepts(challenge), final_url="https://api.example.com/data"
    )
    assert result.status == Status.PASS


def test_scheme_mismatch_fails_on_http_vs_https():
    challenge = _good_challenge()
    challenge["resource"]["url"] = "http://api.example.com/data"
    result = check_scheme_mismatch(
        challenge, extract_accepts(challenge), final_url="https://api.example.com/data"
    )
    assert result.status == Status.FAIL
    assert "forwarded" in result.fix.lower() or "proxy" in result.fix.lower()


def test_scheme_mismatch_skips_when_no_resource():
    challenge = {"accepts": [{}]}
    result = check_scheme_mismatch(
        challenge, extract_accepts(challenge), final_url="https://api.example.com/data"
    )
    assert result.status == Status.SKIP


# --------------------------------------------------------------------------
# check_bazaar_extension
# --------------------------------------------------------------------------


def test_bazaar_extension_passes_when_well_formed():
    challenge = _good_challenge()
    assert check_bazaar_extension(challenge, extract_accepts(challenge)).status == Status.PASS


def test_bazaar_extension_fails_when_missing_entirely():
    challenge = _good_challenge()
    del challenge["extensions"]
    result = check_bazaar_extension(challenge, extract_accepts(challenge))
    assert result.status == Status.FAIL


def test_bazaar_extension_fails_when_input_type_missing():
    challenge = _good_challenge(extensions={"bazaar": {"info": {"input": {}}}})
    result = check_bazaar_extension(challenge, extract_accepts(challenge))
    assert result.status == Status.FAIL
    assert "input.type" in result.detail


def test_bazaar_extension_warns_when_output_missing_type():
    challenge = _good_challenge(
        extensions={
            "bazaar": {"info": {"input": {"type": "http"}, "output": {"example": {}}}}
        }
    )
    result = check_bazaar_extension(challenge, extract_accepts(challenge))
    assert result.status == Status.WARN


# --------------------------------------------------------------------------
# check_description_length
# --------------------------------------------------------------------------


def test_description_length_passes_under_limit():
    challenge = _good_challenge()
    challenge["resource"]["description"] = "short"
    result = check_description_length(challenge, extract_accepts(challenge))
    assert result.status == Status.PASS


def test_description_length_fails_over_limit():
    challenge = _good_challenge()
    challenge["resource"]["description"] = "x" * 501
    result = check_description_length(challenge, extract_accepts(challenge))
    assert result.status == Status.FAIL
    assert "silent" in result.fix.lower()


def test_description_length_boundary_exactly_500_passes():
    challenge = _good_challenge()
    challenge["resource"]["description"] = "x" * 500
    assert check_description_length(challenge, extract_accepts(challenge)).status == Status.PASS


def test_description_length_skips_when_absent_everywhere():
    challenge = {"accepts": [{}]}
    assert check_description_length(challenge, extract_accepts(challenge)).status == Status.SKIP


# --------------------------------------------------------------------------
# check_route_template (opportunistic check 5)
# --------------------------------------------------------------------------


def test_route_template_skips_when_absent():
    challenge = _good_challenge(extensions={"bazaar": {"info": {"input": {"type": "http"}}}})
    result = check_route_template(challenge, extract_accepts(challenge))
    assert result.status == Status.SKIP


def test_route_template_passes_named_params():
    challenge = _good_challenge()
    challenge["extensions"]["bazaar"]["routeTemplate"] = "/sentiment/:symbol"
    result = check_route_template(challenge, extract_accepts(challenge))
    assert result.status == Status.PASS


def test_route_template_fails_bare_wildcard():
    challenge = _good_challenge()
    challenge["extensions"]["bazaar"]["routeTemplate"] = "/sentiment/*"
    result = check_route_template(challenge, extract_accepts(challenge))
    assert result.status == Status.FAIL
    assert "named-parameter" in result.fix.lower()


def test_route_template_passes_fastapi_style_braces():
    challenge = _good_challenge()
    challenge["extensions"]["bazaar"]["routeTemplate"] = "/sentiment/{symbol}"
    result = check_route_template(challenge, extract_accepts(challenge))
    assert result.status == Status.PASS


# --------------------------------------------------------------------------
# run_checks / summarize
# --------------------------------------------------------------------------


def test_run_checks_all_pass_summary():
    challenge = _good_challenge()
    results = run_checks(challenge, final_url="https://api.example.com/data")
    non_informational = [r for r in results if r.check_id != "multiple_accepts_noted"]
    assert all(r.status in (Status.PASS, Status.SKIP) for r in non_informational)
    summary = summarize(results)
    assert "No issues found" in summary


def test_run_checks_reports_multiple_accepts():
    challenge = _good_challenge()
    challenge["accepts"].append(dict(challenge["accepts"][0], scheme="upto"))
    results = run_checks(challenge, final_url="https://api.example.com/data")
    assert "multiple_accepts_noted" in [r.check_id for r in results]


def test_run_checks_surfaces_failures_in_summary():
    challenge = _good_challenge()
    challenge["resource"]["description"] = "x" * 600
    del challenge["extensions"]
    results = run_checks(challenge, final_url="https://api.example.com/data")
    summary = summarize(results)
    assert "issue(s) found" in summary
    assert "bazaar_extension" in summary
    assert "description_length" in summary
