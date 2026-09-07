"""Credential broker integration without live vault or Google access."""

import json
from types import SimpleNamespace

from click.testing import CliRunner
import pytest

import email_cli as cli
import email_auth_store as store


def test_vault_value_restores_private_copy(tmp_path, monkeypatch):
    path = tmp_path / "token.json"
    monkeypatch.setattr(store, "broker", lambda *args: {"token": "vault"})
    store.restore(path, "email", "work")
    assert json.loads(path.read_text()) == {"token": "vault"}


def test_working_copy_has_private_permissions(tmp_path):
    path = tmp_path / "token.json"
    store.write_private(path, {"token": "private"})
    assert path.stat().st_mode & 0o777 == 0o600


def test_failed_save_preserves_refreshed_token(tmp_path, monkeypatch):
    path = tmp_path / "token.json"
    store.save(path, {"token": "refreshed"}, "email", "work")
    monkeypatch.setattr(store, "broker", lambda *args: {"token": "stale"})
    store.restore(path, "email", "work")
    assert json.loads(path.read_text())["token"] == "refreshed"


def test_successful_save_clears_local_pending_marker(tmp_path, monkeypatch):
    path = tmp_path / "token.json"
    monkeypatch.setattr(store, "broker", lambda *args: {"source": "pending"})
    store.save(path, {"token": "refreshed"}, "email", "work")
    assert not store.marker(path, ".pending").exists()


def test_status_never_emits_extra_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "broker", lambda *args: {"token": "secret", "configured": True, "source": "vault"})
    assert "secret" not in json.dumps(store.status(tmp_path / "token", "email", "work"))


def test_signed_out_skips_restoration(tmp_path, monkeypatch):
    path = tmp_path / "token.json"
    store.write_private(store.marker(path, ".signed-out"), {"signed_out": True})
    monkeypatch.setattr(store, "broker", lambda *args: pytest.fail("must not load after logout"))
    assert store.restore(path, "email", "work") is False


def test_sync_imports_legacy_working_copy(tmp_path, monkeypatch):
    path = tmp_path / "token.json"
    store.write_private(path, {"token": "legacy"})
    saved = []
    def broker(action, connector, account, document=None):
        if action == "save":
            saved.append((connector, account, document))
            return {"source": "vault"}
        return None
    monkeypatch.setattr(store, "broker", broker)
    store.sync(path, "email", "work")
    assert saved == [("email", "work", {"token": "legacy"})]


def test_auth_status_cli_reports_legacy_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cli, "CONFIG_PATH", tmp_path / "config.json")
    path = cli.token_path("default")
    store.write_private(path, {"token": "sensitive"})
    result = CliRunner().invoke(cli.cli, ["auth-status", "--account", "default", "--json"])
    assert json.loads(result.output) == {"connector": "email", "account": "default", "source": "local", "configured": True, "pending": True, "last_sync": None}


def test_auth_sync_cli_signals_pending(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cli, "CONFIG_PATH", tmp_path / "config.json")
    result = CliRunner().invoke(cli.cli, ["auth-sync", "--account", "default", "--json"])
    assert result.exit_code == 3


@pytest.mark.parametrize("account", ["../escape", "/root", "..", "back\\slash"])
def test_account_cannot_escape_working_directory(account):
    with pytest.raises(Exception, match="Invalid account"):
        cli.token_path(account)



def test_broker_keeps_secret_out_of_arguments(monkeypatch):
    captured = {}
    def run(command, **kwargs):
        captured.update(command=command, stdin=kwargs["input"])
        return SimpleNamespace(returncode=0, stdout='{"source": "vault"}')
    # The unpatched function exercises the actual process boundary.
    import importlib
    original_broker = importlib.reload(store).broker
    monkeypatch.setattr(store.subprocess, "run", run)
    original_broker("save", "email", "work", {"token": "sensitive"})
    assert (captured["command"], json.loads(captured["stdin"])) == (["claudine-secret", "auth", "save", "email", "--account", "work"], {"token": "sensitive"})


def test_provider_refresh_is_saved_to_broker(tmp_path, monkeypatch):
    from google.oauth2.credentials import Credentials
    path = tmp_path / "accounts" / "work" / "token.json"
    store.write_private(path, {"token": "expired"})
    monkeypatch.setattr(cli, "CONFIG_DIR", tmp_path)
    saved = []
    class Expired:
        expired = True
        refresh_token = "refresh"
        valid = True
        def refresh(self, request):
            self.token = "renewed"
        def to_json(self):
            return json.dumps({"token": self.token})
    monkeypatch.setattr(Credentials, "from_authorized_user_file", lambda *args: Expired())
    def broker(action, connector, account, document=None):
        if action == "save":
            saved.append((connector, account, document))
            return {"source": "vault"}
        return None
    monkeypatch.setattr(store, "broker", broker)
    cli.gmail_credentials("work")
    assert saved == [("email", "work", {"token": "renewed"})]



def test_cli_process_status_without_broker(tmp_path):
    import os
    from pathlib import Path
    import subprocess
    import sys
    script = Path(cli.__file__).resolve()
    result = subprocess.run([sys.executable, "-O", str(script), "auth-status", "--json"],
                            env={**os.environ, "HOME": str(tmp_path), "PATH": ""},
                            capture_output=True, text=True, timeout=20)
    assert (result.returncode, json.loads(result.stdout)) == (0, {"connector": "email", "account": "default", "source": "unavailable", "configured": False, "pending": False, "last_sync": None})

