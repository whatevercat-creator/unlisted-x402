"""Unlisted's own discovery metadata follows the advice it gives customers:
a description that leads with search words, then a "Use when" sentence,
under 500 characters, 1 to 5 tags with the most
important first, a serviceName, and a described input/output schema.
Local only: reads the module constants and the 402 the app actually serves.
"""

import base64
import json

from fastapi.testclient import TestClient

import main
import payment
from tests.test_payment import PAY_TO, FakeFacilitatorClient

BANNED = ["get listed", "guarantee", "will appear", "will be listed"]


def _served_challenge() -> dict:
    client = TestClient(main.create_app(facilitator_client=FakeFacilitatorClient(), pay_to=PAY_TO))
    resp = client.post("/diagnose", json={"url": "https://seller.test/x"})
    assert resp.status_code == 402
    return json.loads(base64.b64decode(resp.headers["payment-required"]))


def test_description_has_a_use_when_sentence_under_500_chars():
    assert "Use when " in payment.DESCRIPTION
    assert len(payment.DESCRIPTION) <= 500


def test_description_leads_with_search_words():
    d = payment.DESCRIPTION
    assert d.startswith("x402 Bazaar listing checker: ")
    assert not d.startswith("Use when")
    assert d.index("Bazaar listing checker") < d.index("Use when ")


def test_description_promises_no_listing_and_no_live_index_check():
    text = payment.DESCRIPTION.lower()
    for phrase in BANNED:
        assert phrase not in text, phrase
    # The live index lookup depends on a deployment setting, so don't claim it.
    assert "index status" not in text


def test_tags_most_important_first_and_within_cdp_limits():
    assert payment.TAGS == ["bazaar", "x402", "diagnostics", "listing", "cdp"]
    assert 1 <= len(payment.TAGS) <= 5
    assert len({t.lower() for t in payment.TAGS}) == len(payment.TAGS)
    assert all(1 <= len(t) <= 32 and t.isascii() and t.isprintable() for t in payment.TAGS)


def test_served_402_carries_the_new_metadata():
    resource = _served_challenge()["resource"]
    assert resource["description"] == payment.DESCRIPTION
    assert resource["tags"] == payment.TAGS  # order matters: CDP keeps the first 5
    assert resource["serviceName"] == "Unlisted"


def test_input_schema_body_is_an_object_with_a_uri_url():
    schema = _served_challenge()["extensions"]["bazaar"]["schema"]
    body = schema["properties"]["input"]["properties"]["body"]
    assert body["type"] == "object"
    assert body["properties"]["url"]["format"] == "uri"
    assert body["required"] == ["url"]


def test_output_schema_describes_check_items_and_optional_next_step():
    out = _served_challenge()["extensions"]["bazaar"]["schema"]["properties"]["output"]
    props = out["properties"]["example"]["properties"]
    items = props["checks"]["items"]
    assert set(items["properties"]) >= {"check_id", "status", "detail", "fix"}
    assert items["required"] == ["check_id", "status", "detail"]
    assert set(items["properties"]["status"]["enum"]) == {"pass", "fail", "warn", "skip", "info"}
    assert "next_step" in props
    assert "next_step" not in out["properties"]["example"]["required"]
    for name in ("bazaar", "url", "checks", "verdict", "next_step"):
        assert props[name].get("description"), name


def test_output_example_still_validates_against_the_output_schema():
    import jsonschema

    ext = payment.BAZAAR_EXTENSION["bazaar"]
    jsonschema.validate(ext["info"]["output"]["example"], ext["schema"]["properties"]["output"]["properties"]["example"])


def test_example_input_still_validates_against_the_input_schema():
    import jsonschema

    ext = payment.BAZAAR_EXTENSION["bazaar"]
    jsonschema.validate(ext["info"]["input"], ext["schema"]["properties"]["input"])
