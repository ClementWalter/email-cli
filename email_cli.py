#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "click>=8.1",
#     "google-api-python-client",
#     "google-auth-httplib2",
#     "google-auth-oauthlib",
# ]
# ///
"""One email CLI for every machine: Gmail API where a token exists, Mail.app where macOS is.

Accounts are names (`default`, `zama`, `icloud`...) mapped in
~/.config/email-cli/config.json to an address and, where relevant, the Mail.app
account name. Each command takes `--account` and `--backend auto|gmail|mailapp`:

  gmail     Google's REST API with an OAuth token stored per account under
            ~/.config/email-cli/accounts/<name>/token.json (the OAuth client is
            the one gdrive-cli already uses). Works from any machine, searches
            with Gmail's query syntax, fast on large mailboxes.
  mailapp   macOS Mail.app driven over JavaScript for Automation. Covers the
            accounts Gmail cannot (iCloud, Outlook, IMAP) but only on a Mac
            with Mail running, and full-text search there is slow: bound it
            with --since and a mailbox.
  auto      gmail when the account has a token, else mailapp on macOS.

Commands: accounts, mailboxes, list, search, read, attachments, send (dry run
unless --yes). Every read supports --json; message ids are the backend's own
(Gmail id, or Mail.app "<account>:<mailbox>:<id>"), stable for the caller.
"""

from __future__ import annotations

import base64
import datetime as dt
import html
import json
import logging
import os
import pathlib
import re
import subprocess
import sys
from email.message import EmailMessage
from email.utils import parsedate_to_datetime

import click

CONFIG_DIR = pathlib.Path.home() / ".config" / "email-cli"
CONFIG_PATH = CONFIG_DIR / "config.json"
GDRIVE_ACCOUNTS = pathlib.Path.home() / ".config" / "gdrive-cli" / "accounts"
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify", "https://www.googleapis.com/auth/gmail.compose"]
IS_MACOS = sys.platform == "darwin"
BACKENDS = ("auto", "gmail", "mailapp")

log = logging.getLogger("email")


# --- config ------------------------------------------------------------------

DEFAULT_CONFIG = {
    "default_account": "default",
    "accounts": {
        "default": {"address": "clement0walter@gmail.com", "mailapp": "Google"},
        "zama": {"address": "clement.walter@zama.ai", "mailapp": "Zama"},
        "kakarot": {"address": "clement@kakarot.org", "mailapp": "Kakarot"},
        "icloud": {"address": "", "mailapp": "iCloud"},
        "outlook": {"address": "clemlaflemme@hotmail.com", "mailapp": "Outlook"},
    },
}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return DEFAULT_CONFIG


def account_config(name: str | None) -> tuple[str, dict]:
    cfg = load_config()
    name = name or cfg.get("default_account", "default")
    try:
        return name, cfg["accounts"][name]
    except KeyError:
        raise click.ClickException(f"unknown account {name!r}; known: {', '.join(cfg['accounts'])}")


def token_path(account: str) -> pathlib.Path:
    return CONFIG_DIR / "accounts" / account / "token.json"


def client_secret_path(account: str) -> pathlib.Path | None:
    """Reuse gdrive-cli's OAuth client: same Google Cloud project, per-account or shared file."""
    for candidate in (
        CONFIG_DIR / "accounts" / account / "client_secret.json",
        CONFIG_DIR / "client_secret.json",
        GDRIVE_ACCOUNTS / account / "client_secret.json",
        GDRIVE_ACCOUNTS / "default" / "client_secret.json",
    ):
        if candidate.is_file():
            return candidate
    return None


def resolve_backend(option: str, account: str, acc: dict, macos: bool = IS_MACOS) -> str:
    if option == "auto":
        if token_path(account).exists():
            return "gmail"
        if macos and acc.get("mailapp"):
            return "mailapp"
        raise click.ClickException(f"account {account!r}: no Gmail token (run `email auth login --account {account}`) and no Mail.app here")
    if option == "mailapp" and not macos:
        raise click.ClickException("the mailapp backend needs macOS")
    return option


# --- shared shapes ----------------------------------------------------------

def item(backend: str, account: str, msg_id: str, ts: int | None, sender: str, to: str, subject: str, snippet: str = "", unread: bool | None = None, mailbox: str = "") -> dict:
    return {"id": msg_id, "backend": backend, "account": account, "mailbox": mailbox, "ts": ts, "from": sender, "to": to, "subject": subject, "snippet": snippet, "unread": unread}


def parse_since(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    when = dt.datetime.fromisoformat(value)
    return when if when.tzinfo else when.astimezone()


def html_to_text(markup: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", markup)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", html.unescape(text)).strip()


# --- gmail backend -------------------------------------------------------------

def gmail_credentials(account: str, interactive: bool = False, login_hint: str | None = None):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    path = token_path(account)
    creds = Credentials.from_authorized_user_file(str(path), GMAIL_SCOPES) if path.exists() else None
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        save_token(creds, account)
    if creds and creds.valid:
        return creds
    if not interactive:
        raise click.ClickException(f"account {account!r} has no valid Gmail token; run `email auth login --account {account}` in a browser-capable terminal")
    from google_auth_oauthlib.flow import InstalledAppFlow

    secret = client_secret_path(account)
    if secret is None:
        raise click.ClickException("no client_secret.json found (email-cli or gdrive-cli config)")
    flow = InstalledAppFlow.from_client_secrets_file(str(secret), GMAIL_SCOPES)
    creds = flow.run_local_server(port=0, **({"login_hint": login_hint} if login_hint else {}))
    save_token(creds, account)
    return creds


def save_token(creds, account: str) -> None:
    path = token_path(account)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(creds.to_json())
    path.chmod(0o600)


def gmail_service(account: str):
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=gmail_credentials(account), cache_discovery=False)


def gmail_labels(svc) -> list[dict]:
    return svc.users().labels().list(userId="me").execute().get("labels", [])


def gmail_label_id(svc, name: str) -> str:
    for label in gmail_labels(svc):
        if label["name"].lower() == name.lower() or label["id"] == name:
            return label["id"]
    raise click.ClickException(f"no Gmail label {name!r}")


def header(msg: dict, name: str) -> str:
    for h in msg.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def gmail_items(svc, account: str, query: str, limit: int, label: str | None) -> list[dict]:
    params = {"userId": "me", "q": query or None, "maxResults": min(limit, 500)}
    if label:
        params["labelIds"] = [gmail_label_id(svc, label)]
    ids = svc.users().messages().list(**{k: v for k, v in params.items() if v is not None}).execute().get("messages", [])
    items = []
    for ref in ids[:limit]:
        msg = svc.users().messages().get(userId="me", id=ref["id"], format="metadata", metadataHeaders=["From", "To", "Subject", "Date"]).execute()
        items.append(item("gmail", account, msg["id"], int(msg["internalDate"]) // 1000, header(msg, "From"), header(msg, "To"), header(msg, "Subject"), msg.get("snippet", ""), "UNREAD" in msg.get("labelIds", []), ",".join(msg.get("labelIds", []))))
    return items


def gmail_body(payload: dict) -> tuple[str, list[dict]]:
    """Prefer text/plain, fall back to stripped HTML; collect attachment parts."""
    plain, rich, attachments = [], [], []

    def walk(part: dict):
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        if part.get("filename"):
            attachments.append({"filename": part["filename"], "mimeType": mime, "size": body.get("size"), "attachmentId": body.get("attachmentId")})
        elif body.get("data"):
            text = base64.urlsafe_b64decode(body["data"]).decode("utf-8", "replace")
            (plain if mime == "text/plain" else rich if mime == "text/html" else plain).append(text)
        for sub in part.get("parts", []):
            walk(sub)

    walk(payload)
    return ("\n".join(plain).strip() or html_to_text("\n".join(rich))), attachments


def gmail_read(svc, account: str, msg_id: str) -> dict:
    msg = svc.users().messages().get(userId="me", id=msg_id, format="full").execute()
    body, attachments = gmail_body(msg["payload"])
    out = item("gmail", account, msg["id"], int(msg["internalDate"]) // 1000, header(msg, "From"), header(msg, "To"), header(msg, "Subject"), msg.get("snippet", ""), "UNREAD" in msg.get("labelIds", []), ",".join(msg.get("labelIds", [])))
    out.update(cc=header(msg, "Cc"), thread_id=msg.get("threadId"), body=body, attachments=attachments)
    return out


def gmail_save_attachments(svc, msg_id: str, out_dir: pathlib.Path) -> list[pathlib.Path]:
    msg = svc.users().messages().get(userId="me", id=msg_id, format="full").execute()
    _, attachments = gmail_body(msg["payload"])
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for att in attachments:
        if not att.get("attachmentId"):
            continue
        data = svc.users().messages().attachments().get(userId="me", messageId=msg_id, id=att["attachmentId"]).execute()["data"]
        target = out_dir / att["filename"]
        target.write_bytes(base64.urlsafe_b64decode(data))
        saved.append(target)
    return saved


def build_message(sender: str, to: str, subject: str, body: str, cc: str | None, attachments: list[pathlib.Path]) -> EmailMessage:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, to, subject
    if cc:
        msg["Cc"] = cc
    msg.set_content(body)
    for path in attachments:
        import mimetypes

        mime, _ = mimetypes.guess_type(path.name)
        maintype, subtype = (mime or "application/octet-stream").split("/", 1)
        msg.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)
    return msg


def gmail_send(svc, msg: EmailMessage) -> str:
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return svc.users().messages().send(userId="me", body={"raw": raw}).execute()["id"]


# --- mailapp backend (JavaScript for Automation) -------------------------------

JXA_PRELUDE = r"""
const Mail = Application("Mail");
function account(name) { const a = Mail.accounts.whose({name: name})(); if (!a.length) throw new Error("no Mail.app account " + name); return a[0]; }
function findMailbox(acc, path) {
  const parts = path.split("/"); let scope = acc.mailboxes; let mb = null;
  for (const p of parts) { const hits = scope.whose({name: p})(); if (!hits.length) throw new Error("no mailbox " + path + " in " + acc.name()); mb = hits[0]; scope = mb.mailboxes; }
  return mb;
}
function walk(mb, prefix, out) { const name = prefix + mb.name(); out.push({path: name, count: mb.messages.length, unread: mb.unreadCount()}); mb.mailboxes().forEach(c => walk(c, name + "/", out)); }
function summary(m, accName, mbPath) {
  return {id: accName + ":" + mbPath + ":" + m.id(), ts: Math.floor(m.dateReceived().getTime() / 1000), from: m.sender(), to: (m.toRecipients().map(r => r.address())).join(", "), subject: m.subject(), unread: !m.readStatus(), mailbox: mbPath};
}
"""


def jxa(script: str, timeout: int = 300) -> object:
    """Run a JXA snippet under Mail.app and return its JSON result."""
    result = subprocess.run(["/usr/bin/osascript", "-l", "JavaScript", "-e", JXA_PRELUDE + script], capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise click.ClickException(f"Mail.app: {result.stderr.strip().splitlines()[-1] if result.stderr.strip() else 'osascript failed'}")
    return json.loads(result.stdout) if result.stdout.strip() else None


def js(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def mailapp_mailboxes(acc_name: str) -> list[dict]:
    return jxa(f"const out = []; account({js(acc_name)}).mailboxes().forEach(mb => walk(mb, '', out)); JSON.stringify(out);")


def mailapp_items(account: str, acc_name: str, mailbox: str, since: dt.datetime | None, limit: int, subject: str | None = None, sender: str | None = None, content: str | None = None) -> list[dict]:
    """Messages of one mailbox, newest first, filtered server-side by Mail.app's `whose`."""
    clauses = []
    if since:
        clauses.append(f"dateReceived: {{_greaterThan: new Date({int(since.timestamp() * 1000)})}}")
    if subject:
        clauses.append(f"subject: {{_contains: {js(subject)}}}")
    if sender:
        clauses.append(f"sender: {{_contains: {js(sender)}}}")
    if content:
        clauses.append(f"content: {{_contains: {js(content)}}}")
    where = "{" + ", ".join(clauses) + "}"
    script = f"""
const acc = account({js(acc_name)}); const mb = findMailbox(acc, {js(mailbox)});
const msgs = {"mb.messages.whose(" + where + ")()" if clauses else "mb.messages()"};
const out = msgs.slice(-{limit}).reverse().map(m => summary(m, {js(acc_name)}, {js(mailbox)}));
JSON.stringify(out);"""
    return [dict(item("mailapp", account, m["id"], m["ts"], m["from"], m["to"], m["subject"], "", m["unread"], m["mailbox"])) for m in jxa(script)]


def mailapp_read(account: str, msg_id: str) -> dict:
    acc_name, mailbox, raw_id = msg_id.split(":", 2)
    script = f"""
const acc = account({js(acc_name)}); const mb = findMailbox(acc, {js(mailbox)});
const m = mb.messages.byId({int(raw_id)});
const s = summary(m, {js(acc_name)}, {js(mailbox)});
s.cc = m.ccRecipients().map(r => r.address()).join(", ");
s.body = m.content();
// Mail.app exposes attachment name reliably; type and size vary by message, so each is best-effort.
function prop(o, k) {{ try {{ return o[k](); }} catch (e) {{ return null; }} }}
s.attachments = m.mailAttachments().map(a => ({{filename: prop(a, "name"), mimeType: prop(a, "mimeType"), size: prop(a, "fileSize")}}));
JSON.stringify(s);"""
    m = jxa(script)
    out = item("mailapp", account, m["id"], m["ts"], m["from"], m["to"], m["subject"], "", m["unread"], m["mailbox"])
    out.update(cc=m["cc"], thread_id=None, body=m["body"], attachments=m["attachments"])
    return out


def mailapp_save_attachments(msg_id: str, out_dir: pathlib.Path) -> list[pathlib.Path]:
    acc_name, mailbox, raw_id = msg_id.split(":", 2)
    out_dir.mkdir(parents=True, exist_ok=True)
    script = f"""
const acc = account({js(acc_name)}); const mb = findMailbox(acc, {js(mailbox)});
const m = mb.messages.byId({int(raw_id)}); const saved = [];
m.mailAttachments().forEach(a => {{ const p = {js(str(out_dir))} + "/" + a.name(); Mail.save(a, {{in: Path(p)}}); saved.push(p); }});
JSON.stringify(saved);"""
    return [pathlib.Path(p) for p in jxa(script)]


def mailapp_send(acc_name: str, sender: str, to: str, subject: str, body: str, cc: str | None, attachments: list[pathlib.Path]) -> None:
    recipients = "".join(f"msg.toRecipients.push(Mail.ToRecipient({{address: {js(a.strip())}}}));" for a in to.split(","))
    ccs = "".join(f"msg.ccRecipients.push(Mail.CcRecipient({{address: {js(a.strip())}}}));" for a in (cc or "").split(",") if a.strip())
    files = "".join(f"msg.attachments.push(Mail.Attachment({{fileName: Path({js(str(p))})}}));" for p in attachments)
    script = f"""
const msg = Mail.OutgoingMessage({{subject: {js(subject)}, content: {js(body)}, sender: {js(sender)}, visible: false}});
Mail.outgoingMessages.push(msg); {recipients}{ccs}{files}
msg.send(); JSON.stringify({{sent: true}});"""
    jxa(script)


# --- CLI ---------------------------------------------------------------------

def emit(items: list[dict], as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps(items, ensure_ascii=False, indent=2))
        return
    for it in items:
        when = dt.datetime.fromtimestamp(it["ts"]).strftime("%Y-%m-%d %H:%M") if it.get("ts") else "                "
        flag = "*" if it.get("unread") else " "
        click.echo(f"{when} {flag} {it['id']:<28} {it['from'][:32]:<32} {it['subject'][:70]}")
        if it.get("snippet"):
            click.echo(f"    {it['snippet'][:140]}")


account_option = click.option("--account", "-a", default=None, help="account name from config (default: config's default_account)")
backend_option = click.option("--backend", default="auto", type=click.Choice(BACKENDS), show_default=True)
json_option = click.option("--json", "as_json", is_flag=True)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("-v", "--verbose", is_flag=True)
def cli(verbose: bool) -> None:
    """Read and send email through Gmail's API or macOS Mail.app, same commands everywhere."""
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING, format="%(name)s %(levelname)s %(message)s", stream=sys.stderr)


@cli.command()
@json_option
def accounts(as_json: bool) -> None:
    """List configured accounts and which backend each would use here."""
    cfg = load_config()
    rows = []
    for name, acc in cfg["accounts"].items():
        rows.append({"name": name, "address": acc.get("address", ""), "mailapp": acc.get("mailapp", ""), "gmail_token": token_path(name).exists(), "default": name == cfg.get("default_account")})
    if as_json:
        click.echo(json.dumps(rows, indent=2))
        return
    for r in rows:
        backend = "gmail" if r["gmail_token"] else ("mailapp" if IS_MACOS and r["mailapp"] else "-")
        click.echo(f"{'*' if r['default'] else ' '} {r['name']:<10} {r['address']:<30} mailapp={r['mailapp'] or '-':<10} → {backend}")


@cli.group()
def auth() -> None:
    """Gmail OAuth tokens."""


@auth.command("login")
@account_option
def auth_login(account: str | None) -> None:
    """Consent once in the browser; the token is saved under ~/.config/email-cli/accounts/<name>/."""
    name, acc = account_config(account)
    creds = gmail_credentials(name, interactive=True, login_hint=acc.get("address") or None)
    click.echo(f"token saved for {name} ({acc.get('address')}) → {token_path(name)}")


@cli.command()
@account_option
@backend_option
@json_option
def mailboxes(account: str | None, backend: str, as_json: bool) -> None:
    """List mailboxes (Mail.app, recursive) or labels (Gmail) of an account."""
    name, acc = account_config(account)
    backend = resolve_backend(backend, name, acc)
    if backend == "gmail":
        rows = [{"path": lb["name"], "id": lb["id"], "type": lb.get("type")} for lb in gmail_labels(gmail_service(name))]
    else:
        rows = mailapp_mailboxes(acc["mailapp"])
    if as_json:
        click.echo(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    for r in rows:
        extra = f"  {r.get('count', '')} msgs, {r.get('unread', '')} unread" if "count" in r else f"  ({r.get('id')})"
        click.echo(f"{r['path']}{extra}")


@cli.command("list")
@click.argument("mailbox", default="INBOX")
@click.option("--since", default=None, help="ISO date/datetime; only messages received at/after it")
@click.option("--limit", default=30, show_default=True)
@account_option
@backend_option
@json_option
def list_cmd(mailbox: str, since: str | None, limit: int, account: str | None, backend: str, as_json: bool) -> None:
    """Recent messages of a mailbox (Mail.app path like "Immo/Poncelet") or Gmail label."""
    name, acc = account_config(account)
    backend = resolve_backend(backend, name, acc)
    when = parse_since(since)
    if backend == "gmail":
        q = f"after:{when.strftime('%Y/%m/%d')}" if when else ""
        emit(gmail_items(gmail_service(name), name, q, limit, mailbox), as_json)
    else:
        emit(mailapp_items(name, acc["mailapp"], mailbox, when, limit), as_json)


@cli.command()
@click.argument("query")
@click.option("--mailbox", default=None, help="restrict to a mailbox/label (Mail.app: required; Gmail: optional)")
@click.option("--since", default=None, help="ISO date; Mail.app searches are slow without it")
@click.option("--limit", default=30, show_default=True)
@click.option("--in", "field", type=click.Choice(["subject", "from", "body"]), default="subject", show_default=True, help="Mail.app only: which field QUERY matches (Gmail uses its own query syntax)")
@account_option
@backend_option
@json_option
def search(query: str, mailbox: str | None, since: str | None, limit: int, field: str, account: str | None, backend: str, as_json: bool) -> None:
    """Search messages. Gmail: full query syntax (from:, subject:, has:attachment, newer_than:30d...). Mail.app: substring on one field."""
    name, acc = account_config(account)
    backend = resolve_backend(backend, name, acc)
    when = parse_since(since)
    if backend == "gmail":
        q = query + (f" after:{when.strftime('%Y/%m/%d')}" if when else "")
        emit(gmail_items(gmail_service(name), name, q, limit, mailbox), as_json)
        return
    if not mailbox:
        raise click.ClickException("Mail.app search needs --mailbox (e.g. INBOX or Immo/Poncelet)")
    kwargs = {"subject": query} if field == "subject" else {"sender": query} if field == "from" else {"content": query}
    emit(mailapp_items(name, acc["mailapp"], mailbox, when, limit, **kwargs), as_json)


@cli.command()
@click.argument("msg_id")
@account_option
@backend_option
@json_option
def read(msg_id: str, account: str | None, backend: str, as_json: bool) -> None:
    """Print one message: headers, plain-text body, attachment names."""
    name, acc = account_config(account)
    backend = "mailapp" if msg_id.count(":") >= 2 else resolve_backend(backend, name, acc)
    msg = gmail_read(gmail_service(name), name, msg_id) if backend == "gmail" else mailapp_read(name, msg_id)
    if as_json:
        click.echo(json.dumps(msg, ensure_ascii=False, indent=2))
        return
    when = dt.datetime.fromtimestamp(msg["ts"]).strftime("%Y-%m-%d %H:%M") if msg.get("ts") else ""
    click.echo(f"From:    {msg['from']}\nTo:      {msg['to']}\nCc:      {msg.get('cc', '')}\nDate:    {when}\nSubject: {msg['subject']}\n")
    click.echo(msg["body"])
    if msg["attachments"]:
        click.echo("\nAttachments: " + ", ".join(a["filename"] for a in msg["attachments"]))


@cli.command()
@click.argument("msg_id")
@click.option("--out", default=".", show_default=True)
@account_option
@backend_option
def attachments(msg_id: str, out: str, account: str | None, backend: str) -> None:
    """Save a message's attachments to --out."""
    name, acc = account_config(account)
    backend = "mailapp" if msg_id.count(":") >= 2 else resolve_backend(backend, name, acc)
    saved = gmail_save_attachments(gmail_service(name), msg_id, pathlib.Path(out)) if backend == "gmail" else mailapp_save_attachments(msg_id, pathlib.Path(out))
    for p in saved:
        click.echo(str(p))


@cli.command()
@click.argument("to")
@click.option("--subject", required=True)
@click.option("--body", default=None, help="text body; reads stdin when omitted")
@click.option("--cc", default=None)
@click.option("--attach", multiple=True, type=click.Path(exists=True, dir_okay=False, path_type=pathlib.Path))
@click.option("--yes", is_flag=True, help="actually send; without it the message is only shown")
@account_option
@backend_option
def send(to: str, subject: str, body: str | None, cc: str | None, attach: tuple[pathlib.Path, ...], yes: bool, account: str | None, backend: str) -> None:
    """Send a plain-text email from an account. Dry run unless --yes."""
    name, acc = account_config(account)
    backend = resolve_backend(backend, name, acc)
    text = body if body is not None else sys.stdin.read()
    sender = acc.get("address") or ""
    click.echo(f"[{'SEND' if yes else 'dry run'}] via {backend} as {sender}\n  to: {to}\n  cc: {cc or '-'}\n  subject: {subject}\n  attachments: {', '.join(p.name for p in attach) or '-'}\n\n{text}", err=not yes)
    if not yes:
        return
    if backend == "gmail":
        click.echo(f"sent: {gmail_send(gmail_service(name), build_message(sender, to, subject, text, cc, list(attach)))}")
    else:
        mailapp_send(acc["mailapp"], sender, to, subject, text, cc, list(attach))
        click.echo("sent via Mail.app")


if __name__ == "__main__":
    cli()
