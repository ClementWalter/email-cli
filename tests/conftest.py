"""Keep authentication tests isolated from real credential stores."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import email_auth_store


@pytest.fixture(autouse=True)
def isolated_broker(monkeypatch):
    monkeypatch.setattr(email_auth_store, "broker", lambda *args, **kwargs: None)

