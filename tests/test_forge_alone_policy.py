"""
Forge Alone policy items: the card statement name and line, Privacy and
Terms links in every page footer, and client job files kept out of git.

Local only: no network, no payments.
"""

import re
import subprocess
from html import unescape
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import fix_page
import main

REPO = Path(__file__).resolve().parent.parent


def test_statement_name_and_line():
    assert fix_page.STATEMENT_NAME == "FORGE ALONE"
    assert fix_page.STATEMENT_LINE == (
        "Your card statement shows FORGE ALONE. "
        "Forge Alone runs Unlisted and the Crypto News Sentiment API."
    )


def test_statement_line_shown_on_fix_page():
    text = unescape(TestClient(main.create_app()).get("/fix").text)
    assert fix_page.STATEMENT_LINE in text
    assert "CRYPTO SENTIMENT" not in text


def test_policy_urls():
    assert fix_page.FORGE_ALONE_PRIVACY_URL == "https://forgealone.com/privacy"
    assert fix_page.FORGE_ALONE_TERMS_URL == "https://forgealone.com/terms"


@pytest.mark.parametrize("path", ["/", "/guide", "/fix", "/fix/sample"])
def test_footer_has_privacy_and_terms(path):
    resp = TestClient(main.create_app()).get(path, headers={"accept": "text/html"})
    assert resp.status_code == 200
    footers = re.findall(r"<footer>(.*?)</footer>", resp.text, re.S)
    assert len(footers) == 1
    footer = footers[0]
    assert f'<a href="{fix_page.FORGE_ALONE_PRIVACY_URL}">Privacy</a>' in footer
    assert f'<a href="{fix_page.FORGE_ALONE_TERMS_URL}">Terms</a>' in footer


def test_no_client_job_files_tracked():
    out = subprocess.run(
        ["git", "ls-files", "--", "docs/listing-fix/jobs/"],
        cwd=REPO, capture_output=True, text=True, check=True,
    )
    assert out.stdout == ""


def test_client_job_files_ignored():
    out = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", "docs/listing-fix/jobs/example.md"],
        cwd=REPO, capture_output=True, text=True,
    )
    assert out.returncode == 0
