#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "click==8.3.3",
#     "google-api-python-client==2.201.0",
#     "google-auth-httplib2==0.4.4",
#     "google-auth-oauthlib==1.5.0",
#     "pytest==9.1.1",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""Gmail access levels: `email auth login --access read|write` and the read-only guard on write commands.

Run: uv run --script tests/test_access.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import click
import pytest
from click.testing import CliRunner

# The modules under test sit at the repo root, one level above tests/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import email_cli as ec

READONLY = "https://www.googleapis.com/auth/gmail.readonly"


@pytest.fixture
def token_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ec, "CONFIG_DIR", tmp_path)
    (tmp_path / "accounts" / "work").mkdir(parents=True)
    return tmp_path / "accounts" / "work"


@pytest.fixture
def read_only(token_dir, monkeypatch):
    """A `work` account whose Gmail token holds gmail.readonly only."""
    (token_dir / "token.json").write_text(json.dumps({"token": "t", "scopes": [READONLY]}))
    monkeypatch.setattr(ec, "account_config", lambda account: ("work", {"address": "me@work.test"}))
    monkeypatch.setattr(ec, "resolve_backend", lambda *a, **k: "gmail")


@pytest.fixture
def consent(tmp_path, monkeypatch):
    """Records the scopes the OAuth flow is started with."""
    requested = []

    class Flow:
        @staticmethod
        def from_client_config(config, scopes):
            requested.append(scopes)
            return Flow()

        def run_local_server(self, **kwargs):
            return SimpleNamespace(to_json=lambda: json.dumps({"token": "fresh"}))

    monkeypatch.setattr(ec, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ec, "require_client", lambda account: {"installed": {}})
    monkeypatch.setitem(sys.modules, "google_auth_oauthlib.flow", SimpleNamespace(InstalledAppFlow=Flow))
    return requested


def run(*args, **kwargs):
    return CliRunner().invoke(ec.cli, list(args), **kwargs)


def test_access_is_read_for_readonly_token(read_only):
    assert ec.gmail_access("work") == "read"


def test_access_is_write_for_modify_token(token_dir):
    (token_dir / "token.json").write_text(json.dumps({"scopes": ec.GMAIL_WRITE_SCOPES}))
    assert ec.gmail_access("work") == "write"


def test_access_is_write_for_token_without_scopes(token_dir):
    (token_dir / "token.json").write_text(json.dumps({"token": "t"}))
    assert ec.gmail_access("work") == "write"


def test_access_is_none_without_token(token_dir):
    assert ec.gmail_access("work") is None


def test_require_write_refuses_read_only(read_only):
    with pytest.raises(click.ClickException, match="read-only"):
        ec.require_gmail_write("work")


def test_send_refuses_read_only(read_only):
    assert "read-only" in run("send", "a@b.test", "--subject", "s", "--body", "b", "--yes").output


def test_reply_refuses_read_only(read_only):
    assert "read-only" in run("reply", "18c0ffee", "--body", "b", "--draft").output


def test_draft_update_refuses_read_only(read_only, monkeypatch):
    monkeypatch.setattr(ec, "gmail_service", lambda name: object())
    assert "read-only" in run("draft-update", "r-1", "--body", "b", "--yes").output


def test_draft_delete_refuses_read_only(read_only, monkeypatch):
    monkeypatch.setattr(ec, "gmail_service", lambda name: object())
    assert "read-only" in run("draft-delete", "r-1", "--yes").output


def test_consent_read_requests_readonly_scope(consent):
    ec.gmail_consent("work", "read")
    assert consent == [[READONLY]]


def test_consent_write_requests_modify_and_compose(consent):
    ec.gmail_consent("work", "write")
    assert consent == [ec.GMAIL_WRITE_SCOPES]


def test_login_prompts_for_access(consent, monkeypatch):
    monkeypatch.setattr(ec, "account_config", lambda account: ("work", {"address": "me@work.test"}))
    run("auth", "login", "-a", "work", input="read\n")
    assert consent == [[READONLY]]


def test_login_access_option_skips_prompt(consent, monkeypatch):
    monkeypatch.setattr(ec, "account_config", lambda account: ("work", {"address": "me@work.test"}))
    assert "Access" not in run("auth", "login", "-a", "work", "--access", "write").output

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, *sys.argv[1:]]))
