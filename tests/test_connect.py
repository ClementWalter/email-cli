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
"""Terminal-free setup: OAuth client discovery, `google-setup`, `connect` and its loopback sign-in.

The sign-in runs end to end against a local stub of Google's token endpoint; the
browser is a thread following the redirect. Offline.

Run: uv run --script tests/test_connect.py
"""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import click
import pytest
from click.testing import CliRunner

# The modules under test sit at the repo root, one level above tests/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import email_cli as ec

READONLY = "https://www.googleapis.com/auth/gmail.readonly"


def desktop_client(token_uri: str = "https://oauth2.googleapis.com/token") -> dict:
    return {"installed": {"client_id": "id.apps.googleusercontent.com", "client_secret": "s3cret", "project_id": "claude-123",
                          "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": token_uri,
                          "redirect_uris": ["http://localhost"]}}


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An empty user: no config, no client, nothing in Downloads, no extension settings."""
    monkeypatch.setattr(ec, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(ec, "CONFIG_PATH", tmp_path / "config" / "config.json")
    monkeypatch.setattr(ec, "GDRIVE_ACCOUNTS", tmp_path / "gdrive" / "accounts")
    monkeypatch.setattr(ec, "DOWNLOADS", tmp_path / "Downloads")
    for name in (ec.CLIENT_ID_ENV, ec.CLIENT_SECRET_ENV, ec.CLIENT_FILE_ENV):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


@pytest.fixture
def downloaded(home):
    """The JSON the Cloud console's Download JSON button saves."""
    (home / "Downloads").mkdir()
    path = home / "Downloads" / "client_secret_id.apps.googleusercontent.com.json"
    path.write_text(json.dumps(desktop_client()))
    return path


@pytest.fixture
def settings(home, monkeypatch):
    monkeypatch.setenv(ec.CLIENT_ID_ENV, "id.apps.googleusercontent.com")
    monkeypatch.setenv(ec.CLIENT_SECRET_ENV, "s3cret")


# --- client discovery ------------------------------------------------------------

def test_client_file_desktop_is_accepted(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps(desktop_client()))
    assert ec.load_client_file(path)["installed"]["client_id"] == "id.apps.googleusercontent.com"


@pytest.mark.parametrize("content", [json.dumps({"web": {}}), json.dumps({"type": "service_account"}), "not json"])
def test_client_file_other_kinds_point_to_desktop_app(tmp_path, content):
    path = tmp_path / "c.json"
    path.write_text(content)
    with pytest.raises(click.ClickException, match="Desktop app|cannot read"):
        ec.load_client_file(path)


def test_no_client_anywhere(home):
    assert ec.client_config("default") is None


def test_client_from_extension_settings(settings):
    assert ec.client_config("default")[1] == "extension settings"


def test_client_settings_build_desktop_shape(settings):
    assert ec.client_config("default")[0]["installed"]["client_secret"] == "s3cret"


def test_client_from_settings_file(home, downloaded, monkeypatch):
    monkeypatch.setenv(ec.CLIENT_FILE_ENV, str(downloaded))
    assert ec.client_config("default")[1] == str(downloaded)


def test_client_found_in_downloads(downloaded):
    assert ec.downloaded_client() == downloaded


def test_newest_download_wins(home, downloaded):
    newer = downloaded.with_name("client_secret_new.json")
    newer.write_text(json.dumps(desktop_client()))
    os.utime(downloaded, (1, 1))
    assert ec.downloaded_client() == newer


def test_adopted_client_is_private(home, downloaded):
    ec.adopt_client_file(downloaded)
    assert oct((home / "config" / "client_secret.json").stat().st_mode & 0o777) == "0o600"


# --- next-step errors --------------------------------------------------------------

def test_no_account_names_setup(home):
    with pytest.raises(click.ClickException, match="google_setup"):
        ec.account_config(None)


def test_unsigned_account_names_connect(home):
    with pytest.raises(click.ClickException, match="call `connect` with account 'work'"):
        ec.resolve_backend("auto", "work", {}, macos=False)


def test_connect_without_client_names_setup(home):
    result = CliRunner().invoke(ec.cli, ["connect", "--json"])
    assert "google_setup" in result.output


# --- config ---------------------------------------------------------------------------

def test_first_account_becomes_default(home):
    ec.register_account("work", address="me@work.test")
    assert ec.load_config()["default_account"] == "work"


def test_registering_keeps_other_fields(home):
    ec.register_account("work", mailapp="Google")
    ec.register_account("work", address="me@work.test")
    assert ec.load_config()["accounts"]["work"] == {"mailapp": "Google", "address": "me@work.test"}


def test_accounts_without_config_skips_mail(home, monkeypatch):
    monkeypatch.setattr(ec, "mailapp_servers", lambda refresh=False: pytest.fail("Mail.app asked"))
    assert CliRunner().invoke(ec.cli, ["accounts", "--json"]).output.strip() == "[]"


# --- google-setup -------------------------------------------------------------------------

def test_google_setup_guide_names_desktop_app(home):
    result = CliRunner().invoke(ec.cli, ["google-setup", "--json"])
    assert "**Desktop app**" in json.loads(result.output)["guide"]


def test_google_setup_reports_no_client(home):
    result = CliRunner().invoke(ec.cli, ["google-setup", "--json"])
    assert json.loads(result.output)["status"]["client_configured"] is False


def test_google_setup_reports_download(downloaded):
    result = CliRunner().invoke(ec.cli, ["google-setup", "--json"])
    assert json.loads(result.output)["status"]["client_in_downloads"] == str(downloaded)


def test_google_setup_reports_project(settings, monkeypatch, tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps(desktop_client()))
    monkeypatch.delenv(ec.CLIENT_ID_ENV)
    monkeypatch.setenv(ec.CLIENT_FILE_ENV, str(path))
    result = CliRunner().invoke(ec.cli, ["google-setup", "--json"])
    assert json.loads(result.output)["status"]["client_project"] == "claude-123"


# --- connect: Mail.app ------------------------------------------------------------------------

@pytest.fixture
def mac(home, monkeypatch):
    monkeypatch.setattr(ec, "IS_MACOS", True)
    monkeypatch.setattr(ec, "mailapp_servers", lambda refresh=False: {"iCloud": "imap.mail.me.com"})


def test_connect_mailapp_registers_account(mac):
    CliRunner().invoke(ec.cli, ["connect", "-a", "icloud", "--mailapp", "iCloud", "--json"])
    assert ec.load_config()["accounts"]["icloud"] == {"mailapp": "iCloud"}


def test_connect_unknown_mailapp_lists_mail_accounts(mac):
    result = CliRunner().invoke(ec.cli, ["connect", "--mailapp", "Outlook"])
    assert "Mail has: iCloud" in result.output


# --- connect: detached Gmail sign-in -------------------------------------------------------------

@pytest.fixture
def spawned(settings, monkeypatch):
    """Records the detached helper's command line; the helper reports it opened the page."""
    calls = []

    def popen(cmd, **kwargs):
        calls.append(cmd)
        ec.write_connect_state(step="waiting", account="default", access="read", url="https://accounts.google.com/x", pid=os.getpid())

    monkeypatch.setattr(ec.subprocess, "Popen", popen)
    return calls


def test_connect_returns_the_sign_in_page(spawned):
    result = CliRunner().invoke(ec.cli, ["connect", "--json"])
    assert json.loads(result.output)["url"] == "https://accounts.google.com/x"


def test_connect_hides_helper_pid(spawned):
    result = CliRunner().invoke(ec.cli, ["connect", "--json"])
    assert "pid" not in json.loads(result.output)


def test_connect_detaches_helper(spawned):
    CliRunner().invoke(ec.cli, ["connect", "-a", "work", "--access", "write", "--json"])
    assert spawned[0][-6:] == ["connect", "--helper", "-a", "work", "--access", "write"]


def test_connect_adopts_downloaded_client(downloaded, monkeypatch):
    monkeypatch.setattr(ec.subprocess, "Popen", lambda cmd, **kw: ec.write_connect_state(step="waiting", url="u", pid=os.getpid()))
    CliRunner().invoke(ec.cli, ["connect", "--json"])
    assert (ec.CONFIG_DIR / "client_secret.json").is_file()


def test_status_without_connect_says_none(home):
    result = CliRunner().invoke(ec.cli, ["connect-status", "--json"])
    assert json.loads(result.output)["step"] == "none"


def test_status_of_dead_helper_is_failed(home):
    ec.write_connect_state(step="waiting", account="default", url="u", pid=2**22 + 12345)
    result = CliRunner().invoke(ec.cli, ["connect-status", "--json"])
    assert "stopped" in result.output


# --- loopback flow against a stub token endpoint ----------------------------------------------------

@pytest.fixture
def token_endpoint():
    """Google's token endpoint answering every code with a read token."""
    seen = []

    class Token(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append(parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode()))
            body = json.dumps({"access_token": "at", "refresh_token": "rt", "expires_in": 3600,
                               "token_type": "Bearer", "scope": seen[-1].get("x_scope", [READONLY])[0]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Token)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/token", seen
    server.shutdown()


@pytest.fixture
def stub_google(home, token_endpoint, monkeypatch):
    # The stub endpoint is plain http on loopback.
    monkeypatch.setenv("OAUTHLIB_INSECURE_TRANSPORT", "1")
    path = home / "client.json"
    path.write_text(json.dumps(desktop_client(token_endpoint[0])))
    monkeypatch.setenv(ec.CLIENT_FILE_ENV, str(path))
    monkeypatch.setattr(ec, "gmail_address", lambda creds: "me@gmail.test")
    return token_endpoint[1]


def browser(**override):
    """A user who consents: follows the redirect with Google's answer, optionally altered."""
    def open_url(url):
        query = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        answer = {"code": "c0de", "state": query["state"], **override}
        target = query["redirect_uri"] + "?" + "&".join(f"{k}={v}" for k, v in answer.items())
        threading.Thread(target=lambda: urllib.request.urlopen(target, timeout=5).read(), daemon=True).start()
    return open_url


def test_flow_stores_token(stub_google):
    ec.connect_flow("default", "read", timeout=5, open_browser=browser())
    assert ec.token_path("default").is_file()


def test_flow_token_is_private(stub_google):
    ec.connect_flow("default", "read", timeout=5, open_browser=browser())
    assert oct(ec.token_path("default").stat().st_mode & 0o777) == "0o600"


def test_flow_token_carries_refresh(stub_google):
    ec.connect_flow("default", "read", timeout=5, open_browser=browser())
    assert json.loads(ec.token_path("default").read_text())["refresh_token"] == "rt"


def test_flow_sends_pkce_verifier(stub_google):
    ec.connect_flow("default", "read", timeout=5, open_browser=browser())
    assert "code_verifier" in stub_google[0]


def test_flow_registers_address(stub_google):
    ec.connect_flow("work", "read", timeout=5, open_browser=browser())
    assert ec.load_config()["accounts"]["work"]["address"] == "me@gmail.test"


def test_flow_reports_done(stub_google):
    ec.connect_flow("default", "read", timeout=5, open_browser=browser())
    assert ec.read_connect_state()["step"] == "done"


def test_flow_denied_names_test_users(stub_google):
    ec.connect_flow("default", "read", timeout=5, open_browser=browser(error="access_denied", code=""))
    assert "test user" in ec.read_connect_state()["error"]


def test_flow_rejects_foreign_state(stub_google):
    ec.connect_flow("default", "read", timeout=5, open_browser=browser(state="forged"))
    assert "did not match" in ec.read_connect_state()["error"]


def test_flow_partial_grant_fails(stub_google):
    ec.connect_flow("default", "write", timeout=5, open_browser=browser())
    assert "tick every box" in ec.read_connect_state()["error"]


def test_flow_times_out(stub_google):
    ec.connect_flow("default", "read", timeout=1, open_browser=lambda url: None)
    assert "no answer from Google" in ec.read_connect_state()["error"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
