"""Pure helpers of email-cli and the Mail.app query builder, with osascript stubbed.

Run from the repo root: PYTHONPATH=. uv run --with pytest --with click --with google-api-python-client --with google-auth-oauthlib pytest -q tests
"""

from __future__ import annotations

import base64
import datetime as dt
import json

import click
import pytest

import email_cli as ec


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def test_html_to_text_strips_tags_and_entities():
    assert ec.html_to_text("<p>Bonjour&nbsp;<b>Loïc</b></p><br><style>x{}</style>") == "Bonjour\xa0Loïc"


def test_gmail_body_prefers_plain_text():
    payload = {"mimeType": "multipart/alternative", "parts": [
        {"mimeType": "text/plain", "body": {"data": b64("plain")}},
        {"mimeType": "text/html", "body": {"data": b64("<b>rich</b>")}},
    ]}
    assert ec.gmail_body(payload)[0] == "plain"


def test_gmail_body_falls_back_to_html():
    payload = {"mimeType": "text/html", "body": {"data": b64("<p>only html</p>")}}
    assert ec.gmail_body(payload)[0] == "only html"


def test_gmail_body_collects_attachments():
    payload = {"mimeType": "multipart/mixed", "parts": [
        {"mimeType": "text/plain", "body": {"data": b64("hi")}},
        {"mimeType": "application/pdf", "filename": "devis.pdf", "body": {"attachmentId": "A1", "size": 10}},
    ]}
    assert ec.gmail_body(payload)[1] == [{"filename": "devis.pdf", "mimeType": "application/pdf", "size": 10, "attachmentId": "A1"}]


def test_header_lookup_is_case_insensitive():
    assert ec.header({"payload": {"headers": [{"name": "subject", "value": "S"}]}}, "Subject") == "S"


def test_resolve_auto_prefers_gmail_token(tmp_path, monkeypatch):
    monkeypatch.setattr(ec, "CONFIG_DIR", tmp_path)
    (tmp_path / "accounts" / "default").mkdir(parents=True)
    (tmp_path / "accounts" / "default" / "token.json").write_text("{}")
    assert ec.resolve_backend("auto", "default", {"mailapp": "Google"}, macos=True) == "gmail"


def test_resolve_auto_falls_back_to_mailapp_on_mac(tmp_path, monkeypatch):
    monkeypatch.setattr(ec, "CONFIG_DIR", tmp_path)
    assert ec.resolve_backend("auto", "icloud", {"mailapp": "iCloud"}, macos=True) == "mailapp"


def test_resolve_auto_fails_loud_on_linux_without_token(tmp_path, monkeypatch):
    monkeypatch.setattr(ec, "CONFIG_DIR", tmp_path)
    with pytest.raises(click.ClickException):
        ec.resolve_backend("auto", "icloud", {"mailapp": "iCloud"}, macos=False)


def test_mailapp_not_available_off_mac():
    with pytest.raises(click.ClickException):
        ec.resolve_backend("mailapp", "default", {"mailapp": "Google"}, macos=False)


def test_mailapp_items_builds_whose_clause_and_items(monkeypatch):
    seen = {}
    def fake_jxa(script, timeout=300):
        seen["script"] = script
        return [{"id": "Google:Immo/Poncelet:42", "ts": 1788000000, "from": "a@b", "to": "c@d", "subject": "S", "unread": True, "mailbox": "Immo/Poncelet"}]
    monkeypatch.setattr(ec, "jxa", fake_jxa)
    since = dt.datetime(2026, 4, 1, tzinfo=dt.timezone.utc)
    items = ec.mailapp_items("default", "Google", "Immo/Poncelet", since, 10, content="Bouny")
    assert items[0]["id"] == "Google:Immo/Poncelet:42" and "content: {_contains: \"Bouny\"}" in seen["script"] and "dateReceived" in seen["script"]


def test_mailapp_items_without_filters_reads_all(monkeypatch):
    seen = {}
    monkeypatch.setattr(ec, "jxa", lambda script, timeout=300: seen.setdefault("s", script) and [])
    ec.mailapp_items("default", "Google", "INBOX", None, 5)
    assert "mb.messages()" in seen["s"] and "whose" not in seen["s"]


def test_build_message_has_attachment():
    import pathlib, tempfile
    with tempfile.TemporaryDirectory() as tmp:
        p = pathlib.Path(tmp) / "note.txt"; p.write_text("x")
        msg = ec.build_message("me@x", "you@y", "Sub", "body", None, [p])
    assert [part.get_filename() for part in msg.iter_attachments()] == ["note.txt"]


def test_parse_since_naive_is_local():
    assert ec.parse_since("2026-09-01").tzinfo is not None


def test_account_config_unknown_fails_loud(monkeypatch, tmp_path):
    monkeypatch.setattr(ec, "CONFIG_PATH", tmp_path / "none.json")
    with pytest.raises(click.ClickException):
        ec.account_config("nope")


class FakeHttpError(Exception):
    def __init__(self, content: bytes):
        self.content = content


def test_explain_disabled_api_points_at_console():
    body = json.dumps({"error": {"message": "Gmail API has not been used in project 1234 before or it is disabled.", "details": [{"reason": "SERVICE_DISABLED", "metadata": {"consumer": "projects/1234"}}], "errors": [{"reason": "accessNotConfigured"}]}}).encode()
    text = ec.explain_http_error(FakeHttpError(body))
    assert "project=1234" in text and "not enabled" in text


def test_explain_other_error_returns_message():
    body = json.dumps({"error": {"message": "Requested entity was not found.", "errors": [{"reason": "notFound"}]}}).encode()
    assert ec.explain_http_error(FakeHttpError(body)) == "Requested entity was not found."


def test_reflow_markdown_joins_hard_wrapped_paragraph():
    text = "Bonjour Madame,\n\nJe me permets de vous relancer au sujet\nde notre dossier crèche, resté sans\nréponse depuis deux mois.\n\nCordialement,\nClément"
    assert ec.reflow_markdown(text) == (
        "Bonjour Madame,\n\n"
        "Je me permets de vous relancer au sujet de notre dossier crèche, resté sans réponse depuis deux mois.\n\n"
        "Cordialement, Clément"
    )


def test_reflow_markdown_keeps_list_items_and_blockquotes_on_their_own_line():
    text = "Contexte:\n- premier point\n- second point\n\n> une citation\n> sur deux lignes"
    assert ec.reflow_markdown(text) == "Contexte:\n- premier point\n- second point\n\n> une citation\n> sur deux lignes"


def test_reflow_markdown_joins_wrapped_numbered_list_item_onto_one_line():
    text = (
        "1. La Chambre régionale des comptes, dans son rapport de 2017\n"
        "   sur l'attribution des places en crèche par la Ville de Paris,\n"
        "   recommandait déjà une analyse statistique annuelle.\n"
        "2. Le 14e arrondissement publie chaque année un bilan\n"
        "   des attributions."
    )
    assert ec.reflow_markdown(text) == (
        "1. La Chambre régionale des comptes, dans son rapport de 2017 sur l'attribution des places en crèche par la Ville de Paris, recommandait déjà une analyse statistique annuelle.\n"
        "2. Le 14e arrondissement publie chaque année un bilan des attributions."
    )


def test_reflow_markdown_collapses_repeated_blank_lines_and_trims_ends():
    assert ec.reflow_markdown("\n\nHello\nworld\n\n\n\nBye\n\n") == "Hello world\n\nBye"


def test_reply_subject_prefixes_once():
    assert ec.reply_subject("Demande d'inscription") == "Re: Demande d'inscription"
    assert ec.reply_subject("Re: Demande d'inscription") == "Re: Demande d'inscription"
    assert ec.reply_subject("RE: Demande d'inscription") == "RE: Demande d'inscription"


def test_reply_references_appends_to_existing_chain():
    assert ec.reply_references("<a@x> <b@x>", "<c@x>") == "<a@x> <b@x> <c@x>"
    assert ec.reply_references("", "<c@x>") == "<c@x>"


def test_gmail_send_sets_thread_id_when_given():
    seen = {}
    class Req:
        def execute(self): return {"id": "sent1"}
    class Msgs:
        def send(self, userId, body):
            seen["body"] = body
            return Req()
    class Users:
        def messages(self): return Msgs()
    class Svc:
        def users(self): return Users()
    msg = ec.build_message("me@x", "you@y", "Re: S", "hi", None, [])
    assert ec.gmail_send(Svc(), msg, thread_id="t1") == "sent1"
    assert seen["body"]["threadId"] == "t1"


def test_gmail_send_omits_thread_id_when_absent():
    class Req:
        def execute(self): return {"id": "sent1"}
    class Msgs:
        def send(self, userId, body):
            assert "threadId" not in body
            return Req()
    class Users:
        def messages(self): return Msgs()
    class Svc:
        def users(self): return Users()
    msg = ec.build_message("me@x", "you@y", "S", "hi", None, [])
    ec.gmail_send(Svc(), msg)


def test_resolve_body_rejects_body_and_file_together(tmp_path):
    p = tmp_path / "draft.md"
    p.write_text("x")
    with pytest.raises(click.ClickException):
        ec.resolve_body("inline", p)


def test_resolve_body_reflows_file(tmp_path):
    p = tmp_path / "draft.md"
    p.write_text("line one\nline two")
    assert ec.resolve_body(None, p) == "line one line two"


def test_resolve_body_reads_stdin_when_neither_given(monkeypatch):
    monkeypatch.setattr(ec.sys, "stdin", __import__("io").StringIO("piped body"))
    assert ec.resolve_body(None, None) == "piped body"


def test_gmail_items_unescape_snippets(monkeypatch):
    class Req:
        def __init__(self, data): self.data = data
        def execute(self): return self.data
    class Msgs:
        def list(self, **k): return Req({"messages": [{"id": "m1"}]})
        def get(self, **k): return Req({"id": "m1", "internalDate": "1788000000000", "snippet": "J&#39;esp&egrave;re", "labelIds": ["INBOX"], "payload": {"headers": [{"name": "From", "value": "a@b"}, {"name": "Subject", "value": "S"}]}})
    class Users:
        def messages(self): return Msgs()
    class Svc:
        def users(self): return Users()
    assert ec.gmail_items(Svc(), "default", "q", 5, None)[0]["snippet"] == "J'espère"
