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
"""Gmail draft management (drafts, draft-show, draft-update, draft-delete) against a stubbed Gmail service.

Run: uv run --script tests/test_drafts.py
"""

from __future__ import annotations

import base64
import email
import json
import sys
from pathlib import Path
from email import policy

import click
import pytest
from click.testing import CliRunner

# The modules under test sit at the repo root, one level above tests/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import email_cli as ec


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode()


class Call:
    """A googleapiclient request: the result only materialises on execute()."""

    def __init__(self, result):
        self.result = result

    def execute(self):
        return self.result


class StubGmail:
    """In-memory Gmail service exposing only the drafts/messages calls the CLI makes."""

    def __init__(self, drafts: dict[str, dict], attachments: dict[str, bytes] | None = None):
        self.drafts_store = drafts
        self.attachments_store = attachments or {}
        self.list_params: dict = {}
        self.updates: list[dict] = []
        self.deleted: list[str] = []

    # svc.users() -> self, then .drafts() / .messages() -> self as well
    def users(self):
        return self

    def drafts(self):
        return DraftsResource(self)

    def messages(self):
        return MessagesResource(self)


class DraftsResource:
    def __init__(self, stub: StubGmail):
        self.stub = stub

    def list(self, **params):
        self.stub.list_params = params
        return Call({"drafts": [{"id": i, "message": {"id": d["message"]["id"]}} for i, d in self.stub.drafts_store.items()]})

    def get(self, userId, id, format):
        return Call(self.stub.drafts_store[id])

    def update(self, userId, id, body):
        self.stub.updates.append(body)
        return Call({"id": id})

    def delete(self, userId, id):
        self.stub.deleted.append(id)
        return Call("")


class MessagesResource:
    def __init__(self, stub: StubGmail):
        self.stub = stub

    def get(self, userId, id, format):
        return Call(next(d["message"] for d in self.stub.drafts_store.values() if d["message"]["id"] == id))

    def attachments(self):
        return AttachmentsResource(self.stub)


class AttachmentsResource:
    def __init__(self, stub: StubGmail):
        self.stub = stub

    def get(self, userId, messageId, id):
        return Call({"data": b64(self.stub.attachments_store[id])})


def draft_resource(draft_id: str = "r-1", message_id: str = "m1", thread_id: str = "t1", attachment_parts: list[dict] | None = None) -> dict:
    headers = [
        {"name": "From", "value": "me@x.org"},
        {"name": "To", "value": "alice@y.org"},
        {"name": "Cc", "value": "bob@y.org"},
        {"name": "Bcc", "value": "archive@x.org"},
        {"name": "Subject", "value": "Re: devis"},
        {"name": "In-Reply-To", "value": "<orig@y.org>"},
        {"name": "References", "value": "<first@y.org> <orig@y.org>"},
    ]
    parts = [{"mimeType": "text/plain", "body": {"data": b64("Old body".encode())}}]
    parts += attachment_parts if attachment_parts is not None else [{"mimeType": "application/pdf", "filename": "devis.pdf", "body": {"attachmentId": "A1", "size": 3}}]
    return {"id": draft_id, "message": {"id": message_id, "threadId": thread_id, "internalDate": "1788000000000", "snippet": "Old body",
                                        "payload": {"mimeType": "multipart/mixed", "headers": headers, "parts": parts}}}


@pytest.fixture
def stub() -> StubGmail:
    return StubGmail({"r-1": draft_resource()}, {"A1": b"pdf"})


@pytest.fixture
def cli_stub(stub, monkeypatch) -> StubGmail:
    monkeypatch.setattr(ec, "account_config", lambda account: ("default", {"address": "me@x.org", "mailapp": "Google"}))
    monkeypatch.setattr(ec, "gmail_service", lambda name: stub)
    return stub


def run(*args: str):
    return CliRunner().invoke(ec.cli, [*args, "--backend", "gmail"], catch_exceptions=False)


def sent_message(stub: StubGmail) -> email.message.EmailMessage:
    raw = stub.updates[0]["message"]["raw"]
    return email.message_from_bytes(base64.urlsafe_b64decode(raw), policy=policy.default)


# --- helpers --------------------------------------------------------------------

def test_draft_read_keeps_thread_id(stub):
    assert ec.gmail_draft_read(stub, "default", "r-1")["thread_id"] == "t1"


def test_draft_read_exposes_in_reply_to(stub):
    assert ec.gmail_draft_read(stub, "default", "r-1")["in_reply_to"] == "<orig@y.org>"


def test_draft_read_body(stub):
    assert ec.gmail_draft_read(stub, "default", "r-1")["body"] == "Old body"


def test_draft_read_attachment_names(stub):
    assert [a["filename"] for a in ec.gmail_draft_read(stub, "default", "r-1")["attachments"]] == ["devis.pdf"]


def test_gmail_drafts_passes_query(stub):
    ec.gmail_drafts(stub, "default", "to:alice", 5)
    assert stub.list_params["q"] == "to:alice"


def test_gmail_drafts_omits_empty_query(stub):
    ec.gmail_drafts(stub, "default", None, 5)
    assert "q" not in stub.list_params


def test_gmail_drafts_respects_limit():
    stub = StubGmail({"r-1": draft_resource("r-1", "m1"), "r-2": draft_resource("r-2", "m2")})
    assert len(ec.gmail_drafts(stub, "default", None, 1)) == 1


def test_draft_summary_has_no_body(stub):
    assert "body" not in ec.draft_summary(ec.gmail_draft_read(stub, "default", "r-1"))


def test_attachment_bytes_by_attachment_id(stub):
    assert ec.gmail_attachment_bytes(stub, "m1", {"filename": "devis.pdf", "attachmentId": "A1"}) == b"pdf"


def test_attachment_bytes_inline_part():
    inline = [{"mimeType": "text/plain", "filename": "note.txt", "body": {"data": b64(b"tiny"), "size": 4}}]
    stub = StubGmail({"r-1": draft_resource(attachment_parts=inline)})
    assert ec.gmail_attachment_bytes(stub, "m1", {"filename": "note.txt", "attachmentId": None}) == b"tiny"


@pytest.fixture
def rebuilt(stub, tmp_path) -> email.message.EmailMessage:
    d = ec.gmail_draft_read(stub, "default", "r-1")
    new = tmp_path / "pv.txt"
    new.write_text("pv")
    msg = ec.draft_message(d, "me@x.org", "alice@y.org", None, "Re: devis", "New body", [new], [(d["attachments"][0], b"pdf")])
    return email.message_from_bytes(msg.as_bytes(), policy=policy.default)


@pytest.mark.parametrize(("name", "value"), [
    ("In-Reply-To", "<orig@y.org>"),
    ("References", "<first@y.org> <orig@y.org>"),
    ("Bcc", "archive@x.org"),
])
def test_draft_message_carries_over_header(rebuilt, name, value):
    assert rebuilt[name] == value


def test_draft_message_has_kept_and_new_attachments(rebuilt):
    assert [p.get_filename() for p in rebuilt.iter_attachments()] == ["pv.txt", "devis.pdf"]


def test_gmail_draft_update_sends_thread_id(stub):
    ec.gmail_draft_update(stub, "r-1", ec.build_message("me@x", "a@y", "S", "b", None, []), "t1")
    assert stub.updates[0]["message"]["threadId"] == "t1"


def test_gmail_draft_update_names_the_draft(stub):
    ec.gmail_draft_update(stub, "r-1", ec.build_message("me@x", "a@y", "S", "b", None, []), "t1")
    assert stub.updates[0]["id"] == "r-1"


def test_drafts_service_refuses_mailapp(monkeypatch):
    monkeypatch.setattr(ec, "resolve_backend", lambda *a, **k: "mailapp")
    with pytest.raises(click.ClickException, match="gmail backend"):
        ec.drafts_service("auto", "icloud", {"mailapp": "iCloud"})


# --- commands -------------------------------------------------------------------

def test_drafts_json_lists_draft_ids(cli_stub):
    assert [r["draft_id"] for r in json.loads(run("drafts", "--json").stdout)] == ["r-1"]


def test_drafts_json_lists_attachment_names(cli_stub):
    assert json.loads(run("drafts", "--json").stdout)[0]["attachments"][0]["filename"] == "devis.pdf"


def test_drafts_text_shows_attachment_count(cli_stub):
    assert "[1 att: devis.pdf]" in run("drafts").stdout


def test_drafts_query_option_reaches_gmail(cli_stub):
    run("drafts", "-q", "subject:devis")
    assert cli_stub.list_params["q"] == "subject:devis"


def test_draft_show_json_has_body(cli_stub):
    assert json.loads(run("draft-show", "r-1", "--json").stdout)["body"] == "Old body"


def test_draft_show_text_lists_attachments(cli_stub):
    assert "Attachments: devis.pdf (3 B)" in run("draft-show", "r-1").stdout


def test_draft_update_dry_run_does_not_write(cli_stub):
    run("draft-update", "r-1", "--body", "New")
    assert cli_stub.updates == []


def test_draft_update_dry_run_reports_dropped_attachments(cli_stub):
    assert "attachments dropped: devis.pdf" in run("draft-update", "r-1", "--body", "New").output


def test_draft_update_keeps_thread(cli_stub):
    run("draft-update", "r-1", "--body", "New", "--yes")
    assert cli_stub.updates[0]["message"]["threadId"] == "t1"


def test_draft_update_keeps_in_reply_to(cli_stub):
    run("draft-update", "r-1", "--body", "New", "--yes")
    assert sent_message(cli_stub)["In-Reply-To"] == "<orig@y.org>"


def test_draft_update_replaces_body(cli_stub):
    run("draft-update", "r-1", "--body", "New body", "--yes")
    assert sent_message(cli_stub).get_body(("plain",)).get_content().strip() == "New body"


def test_draft_update_without_body_keeps_current_body(cli_stub):
    run("draft-update", "r-1", "--subject", "Re: devis v2", "--yes")
    assert sent_message(cli_stub).get_body(("plain",)).get_content().strip() == "Old body"


def test_draft_update_file_is_reflowed(cli_stub, tmp_path):
    draft = tmp_path / "draft.md"
    draft.write_text("line one\nline two\n")
    run("draft-update", "r-1", "--file", str(draft), "--yes")
    assert sent_message(cli_stub).get_body(("plain",)).get_content().strip() == "line one line two"


@pytest.mark.parametrize(("option", "value", "header_name"), [
    ("--subject", "Re: devis v2", "Subject"),
    ("--to", "carol@z.org", "To"),
    ("--cc", "dan@z.org", "Cc"),
])
def test_draft_update_overrides_header(cli_stub, option, value, header_name):
    run("draft-update", "r-1", "--body", "x", option, value, "--yes")
    assert sent_message(cli_stub)[header_name] == value


def test_draft_update_keeps_unchanged_to(cli_stub):
    run("draft-update", "r-1", "--body", "x", "--yes")
    assert sent_message(cli_stub)["To"] == "alice@y.org"


def test_draft_update_drops_attachments_by_default(cli_stub):
    run("draft-update", "r-1", "--body", "x", "--yes")
    assert list(sent_message(cli_stub).iter_attachments()) == []


def test_draft_update_keep_attachments_retains_them(cli_stub):
    run("draft-update", "r-1", "--body", "x", "--keep-attachments", "--yes")
    assert [p.get_content() for p in sent_message(cli_stub).iter_attachments()] == [b"pdf"]


def test_draft_update_attach_adds_file(cli_stub, tmp_path):
    new = tmp_path / "pv.txt"
    new.write_text("pv")
    run("draft-update", "r-1", "--body", "x", "--attach", str(new), "--yes")
    assert [p.get_filename() for p in sent_message(cli_stub).iter_attachments()] == ["pv.txt"]


def test_draft_delete_dry_run_does_not_delete(cli_stub):
    run("draft-delete", "r-1")
    assert cli_stub.deleted == []


def test_draft_delete_dry_run_shows_subject(cli_stub):
    assert "subject: Re: devis" in run("draft-delete", "r-1").output


def test_draft_delete_yes_deletes_every_id(cli_stub):
    cli_stub.drafts_store["r-2"] = draft_resource("r-2", "m2")
    run("draft-delete", "r-1", "r-2", "--yes")
    assert cli_stub.deleted == ["r-1", "r-2"]


def test_draft_commands_refuse_mailapp(cli_stub, monkeypatch):
    monkeypatch.setattr(ec, "resolve_backend", lambda *a, **k: "mailapp")
    result = CliRunner().invoke(ec.cli, ["drafts", "--backend", "mailapp"])
    assert "draft commands need the gmail backend" in result.output

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, *sys.argv[1:]]))
