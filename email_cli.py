#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "click==8.3.3",
#     "google-api-python-client==2.201.0",
#     "google-auth-httplib2==0.4.4",
#     "google-auth-oauthlib==1.5.0",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""One email CLI for every machine: Gmail API where a token exists, Mail.app where macOS is.

Accounts are names (`default`, `work`, `icloud`...) mapped in
~/.config/email-cli/config.json to an address and, where relevant, the Mail.app
account name. Each command takes `--account` and `--backend auto|gmail|mailapp`:

  gmail     Google's REST API with an OAuth token stored per account under
            ~/.config/email-cli/accounts/<name>/token.json (the OAuth client is
            the user's own Desktop app client, shared with gdrive-cli). `email auth login` asks for read
            access (gmail.readonly: no send, no draft writes) or write access. Works from any machine, searches
            with Gmail's query syntax, fast on large mailboxes.
  mailapp   macOS Mail.app driven over JavaScript for Automation. Covers the
            accounts Gmail cannot (iCloud, Outlook, IMAP) but only on a Mac
            with Mail running, and full-text search there is slow: bound it
            with --since and a mailbox.
  auto      gmail when the account has a token, else mailapp on macOS. When Gmail
            times out, is unreachable or rejects the token, mailboxes/list/
            search fall back to mailapp; send never does (a timed-out send may
            have gone through).

Without a terminal: `google-setup` prints the Google Cloud steps (GOOGLE_SETUP.md),
`connect` signs in through a detached loopback OAuth flow or registers a Mail.app
account, and `connect-status` reports how it went.

Commands: accounts, mailboxes, list, search, read, attachments, send, reply, connect,
connect-status, google-setup, and
the Gmail-only drafts, draft-show, draft-update, draft-delete. send, reply,
draft-update and draft-delete are dry runs unless --yes. Every read supports --json; message ids are the backend's own
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
import unicodedata
from email.message import EmailMessage

import click

# Entry points may be symlinked into a shared bin directory.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import email_auth_store as auth_store

CONFIG_DIR = pathlib.Path.home() / ".config" / "email-cli"
CONFIG_PATH = CONFIG_DIR / "config.json"
GDRIVE_ACCOUNTS = pathlib.Path.home() / ".config" / "gdrive-cli" / "accounts"
DOWNLOADS = pathlib.Path.home() / "Downloads"
SETUP_GUIDE = pathlib.Path(__file__).resolve().parent / "GOOGLE_SETUP.md"
# The Claude Desktop extension's settings; the same OAuth client also serves gdrive-cli.
CLIENT_ID_ENV, CLIENT_SECRET_ENV, CLIENT_FILE_ENV = "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_CLIENT_FILE"
CONNECT_TIMEOUT_S = 600
GMAIL_WRITE_SCOPES = ["https://www.googleapis.com/auth/gmail.modify", "https://www.googleapis.com/auth/gmail.compose"]
# Chosen at `email auth login`: a read token is refused by Google itself on send and draft writes.
GMAIL_ACCESS_SCOPES = {"read": ["https://www.googleapis.com/auth/gmail.readonly"], "write": GMAIL_WRITE_SCOPES}
IS_MACOS = sys.platform == "darwin"
BACKENDS = ("auto", "gmail", "mailapp")

log = logging.getLogger("email")


# --- config ------------------------------------------------------------------

DEFAULT_CONFIG = {
    "default_account": "",
    "accounts": {},
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
        if not cfg["accounts"]:
            raise click.ClickException(NO_ACCOUNT)
        raise click.ClickException(f"unknown account {name!r}; known: {', '.join(cfg['accounts'])}")


NO_ACCOUNT = ("no email account is set up yet. From Claude: call `google_setup` and follow it, then `connect` "
              "(or, on a Mac, `connect` with `mailapp` set to an account of the Mail app). "
              "In a terminal: `email connect`.")


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
    CONFIG_PATH.chmod(0o600)


def register_account(name: str, **fields: str) -> None:
    """Add or update an account; the first one becomes the default."""
    cfg = load_config()
    cfg = {**cfg, "accounts": {**cfg.get("accounts", {})}}
    cfg["accounts"][name] = {**cfg["accounts"].get(name, {}), **{k: v for k, v in fields.items() if v}}
    if not cfg.get("default_account"):
        cfg["default_account"] = name
    save_config(cfg)


def token_path(account: str) -> pathlib.Path:
    if not account or account in (".", "..") or "/" in account or "\\" in account:
        raise click.ClickException("Invalid account name")
    return CONFIG_DIR / "accounts" / account / "token.json"


def client_secret_path(account: str) -> pathlib.Path | None:
    """Reuse gdrive-cli's OAuth client: same Google Cloud project, per-account or shared file."""
    auth_store.restore(GDRIVE_ACCOUNTS / account / "client_secret.json", "gdrive-client", account)
    if account != "default":
        auth_store.restore(GDRIVE_ACCOUNTS / "default" / "client_secret.json", "gdrive-client", "default")
    for candidate in (
        CONFIG_DIR / "accounts" / account / "client_secret.json",
        CONFIG_DIR / "client_secret.json",
        GDRIVE_ACCOUNTS / account / "client_secret.json",
        GDRIVE_ACCOUNTS / "default" / "client_secret.json",
        GDRIVE_ACCOUNTS.parent / "client_secret.json",
    ):
        if candidate.is_file():
            return candidate
    return None


def load_client_file(path: pathlib.Path) -> dict:
    """A Desktop app client JSON as Google downloads it; other client types cannot do the loopback flow."""
    try:
        data = json.loads(pathlib.Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise click.ClickException(f"cannot read the Google client file {path}: {exc}")
    if "installed" in data:
        return data
    kind = "a Web application client" if "web" in data else "not a Google OAuth client JSON"
    raise click.ClickException(f"{path} is {kind}; create an OAuth client of type Desktop app (`google_setup`, step 5)")


def downloaded_client() -> pathlib.Path | None:
    """The newest client JSON the Cloud console saved in Downloads."""
    found = sorted(DOWNLOADS.glob("client_secret_*.json"), key=lambda p: p.stat().st_mtime) if DOWNLOADS.is_dir() else []
    return found[-1] if found else None


def client_config(account: str) -> tuple[dict, str] | None:
    """The OAuth client and where it came from: extension settings, then files (Downloads only once `connect` adopts it)."""
    client_id, secret = os.environ.get(CLIENT_ID_ENV, "").strip(), os.environ.get(CLIENT_SECRET_ENV, "").strip()
    if client_id and secret:
        return {"installed": {"client_id": client_id, "client_secret": secret, "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                              "token_uri": "https://oauth2.googleapis.com/token", "redirect_uris": ["http://localhost"]}}, "extension settings"
    if os.environ.get(CLIENT_FILE_ENV, "").strip():
        path = pathlib.Path(os.environ[CLIENT_FILE_ENV].strip()).expanduser()
        return load_client_file(path), str(path)
    path = client_secret_path(account)
    return (load_client_file(path), str(path)) if path else None


NO_CLIENT = ("no Google OAuth client is configured. From Claude: call `google_setup` and follow it (Google Cloud project, "
             "Desktop app client), then fill Client ID and Client secret in the Email extension's settings or call "
             "`connect` with `client_file`. Without Google, a Mac can use the Mail app: `connect` with `mailapp`.")


def require_client(account: str) -> dict:
    found = client_config(account)
    if not found:
        raise click.ClickException(NO_CLIENT)
    return found[0]


def adopt_client_file(path: pathlib.Path) -> None:
    """Keep a private copy so the sign-in survives the user tidying Downloads."""
    auth_store.write_private(CONFIG_DIR / "client_secret.json", load_client_file(path))


GMAIL_IMAP_HOSTS = ("imap.gmail.com", "imap.googlemail.com")
# Mail.app account name -> IMAP host. Hosts rarely change, and asking Mail costs an Apple
# event (and launches Mail), so `email accounts` refreshes it and other commands read it.
MAILAPP_SERVERS_CACHE = CONFIG_DIR / "mailapp-servers.json"


def mailapp_servers(refresh: bool = False) -> dict[str, str]:
    if not refresh and MAILAPP_SERVERS_CACHE.is_file():
        return json.loads(MAILAPP_SERVERS_CACHE.read_text())
    servers = jxa("const out = {}; Mail.accounts().forEach(a => { out[a.name()] = a.serverName() || ''; }); JSON.stringify(out);") or {}
    MAILAPP_SERVERS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    MAILAPP_SERVERS_CACHE.write_text(json.dumps(servers, ensure_ascii=False, indent=2))
    return servers


def is_gmail_hosted(acc: dict, servers: dict[str, str]) -> bool:
    """The Mail.app account syncs from Google's IMAP, so the Gmail API can serve it."""
    return servers.get(acc.get("mailapp", ""), "").lower() in GMAIL_IMAP_HOSTS


def resolve_backend(option: str, account: str, acc: dict, macos: bool = IS_MACOS) -> str:
    if option == "auto":
        if not token_path(account).exists():
            auth_store.restore(token_path(account), "email", account)
        if token_path(account).exists():
            return "gmail"
        if macos and acc.get("mailapp"):
            if is_gmail_hosted(acc, mailapp_servers()):
                log.warning("%s is a Google account served through Mail.app; `email auth login -a %s` makes it use the faster Gmail API", account, account)
            return "mailapp"
        raise click.ClickException(f"account {account!r} is not signed in to Gmail{'' if macos else ' and Mail.app needs macOS'}. "
                                   f"From Claude: call `connect` with account {account!r} (`google_setup` first if Google is not set up). "
                                   f"In a terminal: `email auth login -a {account}`")
    if option == "mailapp" and not macos:
        raise click.ClickException("the mailapp backend needs macOS")
    return option


# --- shared shapes ----------------------------------------------------------

# Gmail search operators; on Mail.app they would be matched as literal substrings.
GMAIL_OPERATOR = re.compile(r"(?<![\w.])-?(from|to|cc|bcc|subject|label|in|is|has|filename|after|before|older|newer|older_than|newer_than|larger|smaller|size|category|list|deliveredto|rfc822msgid):\S", re.IGNORECASE)


def gmail_query_operator(query: str) -> str | None:
    """The first Gmail operator in `query`, if any."""
    m = GMAIL_OPERATOR.search(query)
    return m.group(1) + ":" if m else None


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


STRUCTURAL_LINE = re.compile(r"^\s*(#{1,6}\s|[-*+]\s|\d+[.)]\s|>)")


def reflow_markdown(text: str) -> str:
    """Join a Markdown draft's hard-wrapped paragraphs into the single-line-per-paragraph
    shape a normal email body needs. Blank lines still separate paragraphs; a heading, list
    item or blockquote marker starts a new paragraph even without a blank line before it
    (so consecutive list items don't get glued together), but its own wrapped continuation
    lines still join onto it, so a list item spanning several source lines becomes one."""
    paragraphs: list[str] = []
    current: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            if current:
                paragraphs.append(" ".join(current))
                current = []
            paragraphs.append("")
            continue
        if STRUCTURAL_LINE.match(line) and current:
            paragraphs.append(" ".join(current))
            current = []
        current.append(line)
    if current:
        paragraphs.append(" ".join(current))
    out: list[str] = []
    for p in paragraphs:
        if p == "" and (not out or out[-1] == ""):
            continue
        out.append(p)
    while out and out[0] == "":
        out.pop(0)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def reply_subject(subject: str) -> str:
    return subject if subject.strip().lower().startswith("re:") else f"Re: {subject}"


def reply_references(orig_references: str, orig_message_id: str) -> str:
    return f"{orig_references} {orig_message_id}".strip() if orig_references else orig_message_id


# --- gmail backend -------------------------------------------------------------

def gmail_credentials(account: str):
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    path = token_path(account)
    auth_store.restore(path, "email", account)
    # The token's own scopes: a read token refreshed under write scopes would be refused.
    creds = Credentials.from_authorized_user_file(str(path)) if path.exists() else None
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            save_token(creds, account)
        except RefreshError as exc:
            # A revoked or expired grant cannot be refreshed again: report a re-login
            # instead of raising Google's traceback.
            log.warning("Google no longer accepts the saved sign-in of %s (%s); while the Google app is in Testing it "
                        "lasts 7 days (`google_setup`, step 6)", account, exc)
            creds = None
    if creds and creds.valid:
        return creds
    raise click.ClickException(f"account {account!r} has no valid Gmail token; from Claude call `connect` with account {account!r}, "
                               f"in a terminal run `email auth login --account {account}`")


def gmail_consent(account: str, access: str, login_hint: str | None = None):
    """Browser consent for exactly the scopes of `access`, replacing any stored token."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_config(require_client(account), GMAIL_ACCESS_SCOPES[access])
    creds = flow.run_local_server(port=0, **({"login_hint": login_hint} if login_hint else {}))
    save_token(creds, account)
    return creds


def gmail_access(account: str) -> str | None:
    """`read` or `write` from the stored token's scopes; None without a token."""
    path = token_path(account)
    if not path.exists():
        return None
    scopes = json.loads(path.read_text()).get("scopes")
    # Tokens saved before access levels existed carry the write scopes or none at all.
    return "write" if not scopes or set(GMAIL_WRITE_SCOPES) & set(scopes) else "read"


def require_gmail_write(account: str) -> None:
    """Refuse before any request: Google would reject it with a bare 403."""
    if gmail_access(account) == "read":
        raise click.ClickException(f"account {account!r} is read-only: its Gmail token cannot send or change drafts "
                                   f"(`email auth login -a {account} --access write` grants write access)")


def save_token(creds, account: str) -> None:
    path = token_path(account)
    auth_store.save(path, json.loads(creds.to_json()), "email", account)


GMAIL_TIMEOUT_S = 20


class GmailApi:
    """Thin wrapper turning Google's HTTP errors into one-line CLI errors."""

    def __init__(self, account: str):
        import httplib2
        from google_auth_httplib2 import AuthorizedHttp
        from googleapiclient.discovery import build

        self.account = account
        # A bounded socket timeout turns a stalled Gmail into an error `auto` can fall back from.
        http = AuthorizedHttp(gmail_credentials(account), http=httplib2.Http(timeout=GMAIL_TIMEOUT_S))
        self.svc = build("gmail", "v1", http=http, cache_discovery=False)

    def users(self):
        return self.svc.users()


def gmail_service(account: str):
    return GmailApi(account)


def gmail_unavailable(exc: Exception) -> bool:
    """Gmail is down, unreachable or not authorised — not that the request itself was wrong."""
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    kind = type(exc).__name__
    if kind == "HttpError":
        status = getattr(getattr(exc, "resp", None), "status", 0)
        # A 403 for a disabled API is a setup gap, not a refusal of this request.
        return status in (429, 500, 502, 503, 504) or (status == 403 and explain_http_error(exc).startswith("Gmail API is not enabled"))
    if kind in ("ServerNotFoundError", "TransportError", "RefreshError"):
        return True
    return isinstance(exc, click.ClickException) and "no valid Gmail token" in exc.message


def run_backend(option: str, account: str, acc: dict, gmail, mailapp, macos: bool = IS_MACOS) -> None:
    """Run `gmail` or `mailapp` for the resolved backend. Under `auto`, Mail.app is the fallback
    when Gmail cannot answer; an explicit --backend never switches."""
    backend = resolve_backend(option, account, acc, macos)
    if backend != "gmail":
        mailapp()
        return
    if option != "auto" or not (macos and acc.get("mailapp")):
        gmail()
        return
    try:
        gmail()
    except Exception as exc:
        if not gmail_unavailable(exc):
            raise
        log.warning("Gmail unavailable for %s (%s: %s); falling back to Mail.app", account, type(exc).__name__, exc)
        mailapp()


def client_project_id(account: str | None) -> str:
    """The Cloud project of the OAuth client, from its client_secret.json."""
    try:
        found = client_config(account or "default")
    except click.ClickException:
        return ""
    return found[0]["installed"].get("project_id", "") if found else ""


def explain_http_error(exc, account: str | None = None) -> str:
    """Google's 403 for a disabled API carries the console URL; surface it instead of a traceback."""
    body = getattr(exc, "content", b"") or b""
    try:
        err = json.loads(body).get("error", {})
    except (ValueError, AttributeError):
        err = {}
    reason = ",".join(d.get("reason", "") for d in err.get("details", []) if isinstance(d, dict)) or ",".join(e.get("reason", "") for e in err.get("errors", []))
    if "accessNotConfigured" in reason or "has not been used in project" in str(err.get("message", "")):
        project = ""
        for d in err.get("details", []):
            project = (d.get("metadata") or {}).get("consumer", "").replace("projects/", "") or project
        project = project or client_project_id(account)
        return (f"Gmail API is not enabled on the Google Cloud project of this OAuth client{' (' + project + ')' if project else ''}. "
                f"Enable it once: https://console.developers.google.com/apis/api/gmail.googleapis.com/overview?project={project}")
    return err.get("message") or str(exc)


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
        # Gmail HTML-escapes snippets (&#39;); headers and bodies are not.
        items.append(item("gmail", account, msg["id"], int(msg["internalDate"]) // 1000, header(msg, "From"), header(msg, "To"), header(msg, "Subject"), html.unescape(msg.get("snippet", "")), "UNREAD" in msg.get("labelIds", []), ",".join(msg.get("labelIds", []))))
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
    out = item("gmail", account, msg["id"], int(msg["internalDate"]) // 1000, header(msg, "From"), header(msg, "To"), header(msg, "Subject"), html.unescape(msg.get("snippet", "")), "UNREAD" in msg.get("labelIds", []), ",".join(msg.get("labelIds", [])))
    out.update(cc=header(msg, "Cc"), thread_id=msg.get("threadId"), body=body, attachments=attachments, message_id=header(msg, "Message-ID"), references=header(msg, "References"))
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


def gmail_send(svc, msg: EmailMessage, thread_id: str | None = None) -> str:
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    body = {"raw": raw}
    if thread_id:
        body["threadId"] = thread_id
    return svc.users().messages().send(userId="me", body=body).execute()["id"]


def gmail_draft(svc, msg: EmailMessage, thread_id: str | None = None) -> str:
    """Store msg as a Gmail draft, attached to thread_id so it opens as a reply in that thread."""
    message = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
    if thread_id:
        message["threadId"] = thread_id
    return svc.users().drafts().create(userId="me", body={"message": message}).execute()["id"]


def gmail_draft_read(svc, account: str, draft_id: str) -> dict:
    """One draft as a `read`-shaped dict plus the headers an update must carry over."""
    draft = svc.users().drafts().get(userId="me", id=draft_id, format="full").execute()
    msg = draft["message"]
    body, attachments = gmail_body(msg.get("payload", {}))
    out = item("gmail", account, draft["id"], int(msg.get("internalDate", 0)) // 1000 or None, header(msg, "From"), header(msg, "To"), header(msg, "Subject"), html.unescape(msg.get("snippet", "")))
    out.update(draft_id=draft["id"], message_id=msg["id"], thread_id=msg.get("threadId"), cc=header(msg, "Cc"), bcc=header(msg, "Bcc"),
               in_reply_to=header(msg, "In-Reply-To"), references=header(msg, "References"), body=body, attachments=attachments)
    return out


def gmail_drafts(svc, account: str, query: str | None, limit: int) -> list[dict]:
    """Drafts matching a Gmail query, newest first (Gmail's own order)."""
    params = {"userId": "me", "maxResults": min(limit, 500)}
    if query:
        params["q"] = query
    refs = svc.users().drafts().list(**params).execute().get("drafts", [])
    return [gmail_draft_read(svc, account, ref["id"]) for ref in refs[:limit]]


def gmail_attachment_bytes(svc, message_id: str, att: dict) -> bytes:
    """Large parts are fetched by attachmentId; small ones carry their data inline in the part."""
    if att.get("attachmentId"):
        data = svc.users().messages().attachments().get(userId="me", messageId=message_id, id=att["attachmentId"]).execute()["data"]
        return base64.urlsafe_b64decode(data)
    stack = [svc.users().messages().get(userId="me", id=message_id, format="full").execute()["payload"]]
    while stack:
        part = stack.pop()
        if part.get("filename") == att["filename"] and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"])
        stack.extend(part.get("parts", []))
    raise click.ClickException(f"attachment {att['filename']!r} of draft message {message_id} has no data")


def draft_message(draft: dict, sender: str, to: str, cc: str | None, subject: str, body: str, attachments: list[pathlib.Path], kept: list[tuple[dict, bytes]]) -> EmailMessage:
    """Rebuild a draft's MIME message; Bcc and reply headers are copied so it stays the same reply."""
    msg = build_message(sender, to, subject, body, cc, attachments)
    if draft.get("bcc"):
        msg["Bcc"] = draft["bcc"]
    if draft.get("in_reply_to"):
        msg["In-Reply-To"] = draft["in_reply_to"]
    if draft.get("references"):
        msg["References"] = draft["references"]
    for att, data in kept:
        maintype, subtype = (att.get("mimeType") or "application/octet-stream").split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=att["filename"])
    return msg


def gmail_draft_update(svc, draft_id: str, msg: EmailMessage, thread_id: str | None) -> str:
    """Replace a draft's message in place; threadId keeps it a reply in its thread."""
    message = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
    if thread_id:
        message["threadId"] = thread_id
    return svc.users().drafts().update(userId="me", id=draft_id, body={"id": draft_id, "message": message}).execute()["id"]


def gmail_draft_delete(svc, draft_id: str) -> None:
    svc.users().drafts().delete(userId="me", id=draft_id).execute()


# --- mailapp backend (JavaScript for Automation) -------------------------------

# Each account names its sent folder in its own locale; first hit wins, so the
# live "Messages envoyés" beats a legacy "Sent Items" on the same account.
MAILAPP_SENT_NAMES = ["Messages envoyés", "Sent Mail", "Éléments envoyés", "Sent Messages", "Sent", "Sent Items"]

JXA_PRELUDE = r"""
const Mail = Application("Mail");
function account(name) { const a = Mail.accounts.whose({name: name})(); if (!a.length) throw new Error("no Mail.app account " + name); return a[0]; }
function findMailbox(acc, path) {
  const parts = path.split("/"); let scope = acc.mailboxes; let mb = null;
  for (const p of parts) { const hits = scope.whose({name: p})(); if (!hits.length) throw new Error("no mailbox " + path + " in " + acc.name() + " (top level: " + acc.mailboxes.name().join(", ") + ")"); mb = hits[0]; scope = mb.mailboxes; }
  return mb;
}
const SENT_NAMES = __SENT_NAMES__;
function mailboxPath(acc, path) {
  if (path.toLowerCase() !== "sent") return path;
  const top = acc.mailboxes.name();
  // Some accounts (Outlook) store names decomposed (E + U+0301), so compare in NFC.
  let hit; for (const n of SENT_NAMES) { hit = top.find(t => t.normalize("NFC") === n); if (hit) break; }
  if (!hit) throw new Error("no sent mailbox in " + acc.name() + " (top level: " + top.join(", ") + ")");
  return hit;
}
function walk(mb, prefix, out) { const name = prefix + mb.name(); out.push({path: name, count: mb.messages.length, unread: mb.unreadCount()}); mb.mailboxes().forEach(c => walk(c, name + "/", out)); }
function summary(m, accName, mbPath) {
  return {id: accName + ":" + mbPath + ":" + m.id(), ts: Math.floor(m.dateReceived().getTime() / 1000), from: m.sender(), to: (m.toRecipients().map(r => r.address())).join(", "), subject: m.subject(), unread: !m.readStatus(), mailbox: mbPath};
}
"""
JXA_PRELUDE = JXA_PRELUDE.replace("__SENT_NAMES__", json.dumps(MAILAPP_SENT_NAMES, ensure_ascii=False))


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


# Bodies Mail.app reads for one `--in body` search; past it the search is refused, since
# each body is fetched (often from the server) on Mail's main thread.
MAILAPP_BODY_SCAN_MAX = 500


def mailapp_items(account: str, acc_name: str, mailbox: str, since: dt.datetime | None, limit: int, subject: str | None = None, sender: str | None = None, content: str | None = None) -> list[dict]:
    """Messages of one mailbox, newest first, filtered server-side by Mail.app's `whose`."""
    clauses = []
    if since:
        clauses.append(f"dateReceived: {{_greaterThan: new Date({int(since.timestamp() * 1000)})}}")
    if subject:
        clauses.append(f"subject: {{_contains: {js(subject)}}}")
    if sender:
        clauses.append(f"sender: {{_contains: {js(sender)}}}")
    where = "{" + ", ".join(clauses) + "}"
    select = f"msgs.slice(0, {limit})"
    if content is not None:
        # A `content` clause inside `whose` loads every body in one Apple event, freezing Mail's
        # UI until it ends; one content() call per message lets Mail handle events in between.
        select = f"""(() => {{
  if (msgs.length > {MAILAPP_BODY_SCAN_MAX}) throw new Error("body search would scan " + msgs.length + " messages (max {MAILAPP_BODY_SCAN_MAX}); narrow it with a later --since or a smaller --mailbox");
  const needle = {js(content.lower())}; const hits = [];
  for (const m of msgs) {{ if (hits.length >= {limit}) break; if ((m.content() || "").toLowerCase().includes(needle)) hits.push(m); }}
  return hits;
}})()"""
    script = f"""
const acc = account({js(acc_name)}); const path = mailboxPath(acc, {js(mailbox)}); const mb = findMailbox(acc, path);
const msgs = {"mb.messages.whose(" + where + ")()" if clauses else "mb.messages()"};
// Mail.app returns a mailbox's messages newest first.
const out = {select}.map(m => summary(m, {js(acc_name)}, path));
JSON.stringify(out);"""
    return [dict(item("mailapp", account, m["id"], m["ts"], m["from"], m["to"], m["subject"], "", m["unread"], m["mailbox"])) for m in jxa(script)]


def gmail_body_query(query: str, mailbox: str | None, since: dt.datetime | None) -> str:
    """Gmail query equivalent to a Mail.app body search. Mail.app folder names are not Gmail
    labels, so only a sent folder narrows it; any other mailbox widens to all mail."""
    q = js(query)
    if mailbox and (mailbox.lower() == "sent" or unicodedata.normalize("NFC", mailbox) in MAILAPP_SENT_NAMES):
        q += " in:sent"
    if since:
        q += f" after:{since.strftime('%Y/%m/%d')}"
    return q


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


def mailapp_reply(acc_name: str, mailbox: str, raw_id: str, body: str, cc: str | None, attachments: list[pathlib.Path]) -> None:
    """Mail.app's own `reply` command builds the outgoing message already addressed and
    threaded (In-Reply-To/References, same subject) from the original; we only set the
    content and let it send. Not exercised on this (Linux) box - verify on a Mac before
    trusting it blindly, same as the rest of the mailapp backend."""
    ccs = "".join(f"msg.ccRecipients.push(Mail.CcRecipient({{address: {js(a.strip())}}}));" for a in (cc or "").split(",") if a.strip())
    files = "".join(f"msg.attachments.push(Mail.Attachment({{fileName: Path({js(str(p))})}}));" for p in attachments)
    script = f"""
const acc = account({js(acc_name)}); const mb = findMailbox(acc, {js(mailbox)});
const m = mb.messages.byId({int(raw_id)});
const msg = m.reply({{openingWindow: false}});
msg.content = {js(body)};
{ccs}{files}
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


@click.group(context_settings={"help_option_names": ["-h", "--help"]}, epilog="\b\nExamples:\n  email accounts\n  email search \"from:alice newer_than:30d\" -a default --json\n  email read 18f3a2b9c0d1e2f3 -a default")
@click.option("-v", "--verbose", is_flag=True)
def cli(verbose: bool) -> None:
    """Read and send email through Gmail's API or macOS Mail.app, same commands everywhere."""
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING, format="%(name)s %(levelname)s %(message)s", stream=sys.stderr)
    logging.getLogger("googleapiclient.http").setLevel(logging.ERROR)


def main() -> None:
    try:
        cli.main(standalone_mode=False)
    except click.ClickException as exc:
        exc.show()
        sys.exit(exc.exit_code)
    except click.Abort:
        sys.exit(130)
    except Exception as exc:  # Google API errors reach here as HttpError
        if type(exc).__name__ == "HttpError":
            account = next((sys.argv[i + 1] for i, a in enumerate(sys.argv) if a in ("-a", "--account") and i + 1 < len(sys.argv)), None)
            click.echo(f"Error: {explain_http_error(exc, account)}", err=True)
            sys.exit(1)
        raise


@cli.command(epilog="\b\nExamples:\n  email accounts\n  email accounts --json")
@json_option
def accounts(as_json: bool) -> None:
    """List configured accounts and which backend each would use here."""
    cfg = load_config()
    # Asking Mail launches it: only when an account is served by Mail.app.
    servers = mailapp_servers(refresh=True) if IS_MACOS and any(a.get("mailapp") for a in cfg["accounts"].values()) else {}
    if not cfg["accounts"]:
        log.warning(NO_ACCOUNT)
    rows = []
    for name, acc in cfg["accounts"].items():
        rows.append({"name": name, "address": acc.get("address", ""), "mailapp": acc.get("mailapp", ""), "gmail_token": token_path(name).exists(),
                     "gmail_access": gmail_access(name),
                     "gmail_hosted": is_gmail_hosted(acc, servers), "default": name == cfg.get("default_account")})
    if as_json:
        click.echo(json.dumps(rows, indent=2))
        return
    for r in rows:
        backend = f"gmail ({r['gmail_access']})" if r["gmail_token"] else ("mailapp" if IS_MACOS and r["mailapp"] else "-")
        hint = f"  (Google account: email auth login -a {r['name']})" if r["gmail_hosted"] and not r["gmail_token"] else ""
        click.echo(f"{'*' if r['default'] else ' '} {r['name']:<10} {r['address']:<30} mailapp={r['mailapp'] or '-':<10} → {backend}{hint}")


@cli.group(epilog="\b\nExamples:\n  email auth login -a default --access read\n  email auth status -a default --json")
def auth() -> None:
    """Gmail OAuth tokens."""


@auth.command("login", epilog="\b\nExamples:\n  email auth login -a default --access read\n  email auth login -a work --access write")
@account_option
@click.option("--access", type=click.Choice(sorted(GMAIL_ACCESS_SCOPES)), prompt="Access (read: read-only, no send or drafts; write: read, send, drafts)",
              help="read grants gmail.readonly only; write grants gmail.modify + gmail.compose (send, drafts)")
def auth_login(account: str | None, access: str) -> None:
    """Consent in the browser for read or write access; the token is saved under ~/.config/email-cli/accounts/<name>/."""
    name, acc = account_config(account)
    gmail_consent(name, access, login_hint=acc.get("address") or None)
    click.echo(f"{access} token saved for {name} ({acc.get('address')}) → {token_path(name)}")


@auth.command("status", epilog="\b\nExamples:\n  email auth status -a default\n  email auth-status -a default --json")
@account_option
@json_option
def auth_status(account: str | None, as_json: bool) -> None:
    """Report credential source and pending vault synchronization without secrets."""
    name, _ = account_config(account)
    result = auth_store.status(token_path(name), "email", name)
    click.echo(json.dumps(result) if as_json else f"{name}: {result['source']}")


@auth.command("sync", epilog="\b\nExamples:\n  email auth sync -a default\n  email auth-sync -a default --json")
@account_option
@json_option
def auth_sync(account: str | None, as_json: bool) -> None:
    """Import or synchronize the selected Gmail account with the vault."""
    name, _ = account_config(account)
    result = auth_store.sync(token_path(name), "email", name)
    click.echo(json.dumps(result) if as_json else f"{name}: {result['source']}")
    if result["pending"] or not result["configured"]:
        raise click.exceptions.Exit(3)


cli.add_command(auth_status, "auth-status")
cli.add_command(auth_sync, "auth-sync")


# --- terminal-free setup -------------------------------------------------------

def connect_state_path() -> pathlib.Path:
    return CONFIG_DIR / "connect.json"


def write_connect_state(**state) -> None:
    auth_store.write_private(connect_state_path(), state)


def read_connect_state() -> dict:
    path = connect_state_path()
    return json.loads(path.read_text()) if path.is_file() else {}


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def gmail_address(creds) -> str:
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=creds, cache_discovery=False).users().getProfile(userId="me").execute()["emailAddress"]


CONNECTED_PAGE = (b"<!doctype html><meta charset=utf-8><title>Email connected</title>"
                  b"<body style='font-family:system-ui;margin:3em'><h1>Done</h1><p>You can close this tab and go back to Claude.</p>")


def connect_flow(account: str, access: str, timeout: float = CONNECT_TIMEOUT_S, open_browser=None) -> None:
    """Installed-app loopback flow: serve 127.0.0.1, wait for Google's redirect, store the token.

    Progress goes to the connect state file, the only channel a detached run has."""
    import secrets
    import time
    import webbrowser
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlparse

    from google_auth_oauthlib.flow import InstalledAppFlow

    received: dict = {}

    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self):
            query = parse_qs(urlparse(self.path).query)
            if "code" not in query and "error" not in query:
                self.send_error(404)
                return
            received.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(CONNECTED_PAGE)

        def log_message(self, *args):
            pass

    try:
        server = HTTPServer(("127.0.0.1", 0), Redirect)
        server.timeout = 1
        flow = InstalledAppFlow.from_client_config(require_client(account), GMAIL_ACCESS_SCOPES[access],
                                                   redirect_uri=f"http://127.0.0.1:{server.server_port}/")
        hint = load_config().get("accounts", {}).get(account, {}).get("address")
        # prompt=consent makes Google return a refresh token even on a repeat sign-in.
        url, state = flow.authorization_url(access_type="offline", prompt="consent", state=secrets.token_urlsafe(16),
                                            **({"login_hint": hint} if hint else {}))
        write_connect_state(step="waiting", account=account, access=access, url=url, pid=os.getpid())
        (open_browser or webbrowser.open)(url)
        deadline = time.monotonic() + timeout
        while not received and time.monotonic() < deadline:
            server.handle_request()
        server.server_close()
        if not received:
            raise click.ClickException(f"no answer from Google within {int(timeout)} s; call `connect` again")
        if received.get("state") != state:
            raise click.ClickException("the answer from Google did not match this sign-in; call `connect` again")
        if "error" in received:
            raise click.ClickException(f"Google refused: {received['error']} (access_denied: the user cancelled, or the "
                                       "address is not a test user, `google_setup` step 4)")
        # Google lets the user untick scopes; oauthlib would otherwise raise on any scope difference.
        os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
        token = flow.fetch_token(code=received["code"])
        granted = token.get("scope", "")
        granted = set(granted.split() if isinstance(granted, str) else granted)
        if not set(GMAIL_ACCESS_SCOPES[access]) <= granted:
            raise click.ClickException("Google granted only part of the access: call `connect` again and tick every box")
        save_token(flow.credentials, account)
        address = gmail_address(flow.credentials)
        register_account(account, address=address)
        write_connect_state(step="done", account=account, access=access, address=address)
    except Exception as exc:  # noqa: BLE001 — the state file is the detached run's only reader
        message = exc.message if isinstance(exc, click.ClickException) else f"{type(exc).__name__}: {exc}"
        write_connect_state(step="failed", account=account, access=access, error=message)


def stop_previous_connect() -> None:
    """A newer sign-in replaces a pending one; both would race on the state file."""
    import signal

    state = read_connect_state()
    if state.get("step") == "waiting" and pid_alive(state.get("pid")):
        os.kill(state["pid"], signal.SIGTERM)


@cli.command("google-setup", epilog="\b\nExamples:\n  email google-setup\n  email google-setup --json")
@json_option
def google_setup(as_json: bool) -> None:
    """Step-by-step Google Cloud setup (project, Gmail API, Desktop app OAuth client) for Claude to walk the user through, and what is configured here."""
    found = client_config("default")
    pending_download = found is None and downloaded_client() is not None
    status = {"client_configured": found is not None, "client_source": found[1] if found else None,
              "client_project": found[0]["installed"].get("project_id") if found else None,
              "client_in_downloads": str(downloaded_client()) if pending_download else None,
              "accounts": {name: {"address": acc.get("address", ""), "gmail_token": token_path(name).exists(),
                                  "mailapp": acc.get("mailapp", "")} for name, acc in load_config()["accounts"].items()}}
    guide = SETUP_GUIDE.read_text()
    if as_json:
        click.echo(json.dumps({"status": status, "guide": guide}, indent=2))
        return
    click.echo(guide)
    click.echo("## Here now\n")
    click.echo(f"- OAuth client: {status['client_source'] or 'none'}{' (project ' + status['client_project'] + ')' if status['client_project'] else ''}")
    if pending_download:
        click.echo(f"- Downloaded client JSON, adopted by `connect`: {status['client_in_downloads']}")
    for name, acc in status["accounts"].items():
        click.echo(f"- account {name}: {acc['address'] or '-'}, gmail={'yes' if acc['gmail_token'] else 'no'}, mailapp={acc['mailapp'] or '-'}")


@cli.command(epilog="\b\nExamples:\n  email connect\n  email connect -a work --access write\n"
                    "  email connect --client-file ~/Downloads/client_secret_123.apps.googleusercontent.com.json\n"
                    "  email connect -a icloud --mailapp iCloud")
@click.option("--account", "-a", default=None, help="name for this mailbox (default: the configured default, else 'default')")
@click.option("--access", type=click.Choice(sorted(GMAIL_ACCESS_SCOPES)), default="read", show_default=True,
              help="read: gmail.readonly only; write: also send, reply and drafts")
@click.option("--client-file", type=click.Path(exists=True, dir_okay=False, path_type=pathlib.Path), default=None,
              help="the Desktop app client JSON downloaded from Google Cloud; kept privately for later sign-ins")
@click.option("--mailapp", default=None, help="macOS: use this Mail app account instead of Google (no Google project needed)")
@click.option("--wait", "wait_s", default=0, show_default=True, help="seconds to wait for the sign-in to finish (0: return once the page is open)")
@click.option("--helper", is_flag=True, hidden=True, help="Run the sign-in in this process.")
@json_option
def connect(account: str | None, access: str, client_file: pathlib.Path | None, mailapp: str | None, wait_s: int, helper: bool, as_json: bool) -> None:
    """Sign in to Gmail in the browser, no terminal needed, or register a Mail app account.

    Opens Google's sign-in page and returns at once; the page waits 10 minutes. Check
    the result with `connect-status`. The Google OAuth client comes from the
    GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET settings, --client-file,
    ~/.config/email-cli, or the newest client_secret_*.json in Downloads.
    """
    import time

    name = account or load_config().get("default_account") or "default"
    token_path(name)
    if helper:
        connect_flow(name, access)
        return
    if mailapp:
        if not IS_MACOS:
            raise click.ClickException("the Mail app backend needs macOS; use Google instead (`google_setup`)")
        servers = mailapp_servers(refresh=True)
        if mailapp not in servers:
            raise click.ClickException(f"no Mail app account {mailapp!r}; Mail has: {', '.join(servers) or 'none'}")
        register_account(name, mailapp=mailapp)
        result = {"step": "done", "account": name, "mailapp": mailapp}
        click.echo(json.dumps(result) if as_json else f"account {name} now reads Mail app account {mailapp!r}")
        return
    if client_file or (client_config(name) is None and downloaded_client()):
        adopt_client_file(client_file or downloaded_client())
    require_client(name)
    stop_previous_connect()
    write_connect_state(step="starting", account=name, access=access)
    # Detached so the sign-in outlives this command, which an MCP client waits on.
    subprocess.Popen([sys.executable, str(pathlib.Path(__file__).resolve()), "connect", "--helper", "-a", name, "--access", access],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + max(wait_s, 15)
    state = read_connect_state()
    while time.monotonic() < deadline:
        state = read_connect_state()
        if state.get("step") in ("done", "failed") or (state.get("step") == "waiting" and not wait_s):
            break
        time.sleep(0.2)
    emit_connect_state(state, as_json)


def emit_connect_state(state: dict, as_json: bool) -> None:
    state = {k: v for k, v in state.items() if k != "pid"} or {"step": "none", "next": "call `connect`"}
    step = state.get("step")
    if as_json:
        click.echo(json.dumps(state))
    elif step == "waiting":
        click.echo(f"Google's sign-in page is open in the browser ({state['url']}). Choose the account; on \"Google hasn't "
                   "verified this app\" click Continue, or Advanced then Go to … (unsafe); tick every box and Continue. "
                   "Then check with `connect-status`.")
    elif step == "done":
        click.echo(f"account {state['account']} connected to Gmail as {state.get('address')} ({state.get('access')} access)")
    elif step == "failed":
        raise click.ClickException(f"sign-in of account {state.get('account')} failed: {state.get('error')}")
    elif step == "starting":
        raise click.ClickException("the sign-in did not start; run `email connect --helper` in a terminal to see why")
    else:
        click.echo("no sign-in started; call `connect`")


@cli.command("connect-status", epilog="\b\nExamples:\n  email connect-status\n  email connect-status --json")
@json_option
def connect_status(as_json: bool) -> None:
    """Result of the last `connect`: waiting for the user, done (with the address), or failed (with why)."""
    state = read_connect_state()
    if state.get("step") == "waiting" and not pid_alive(state.get("pid")):
        state = {**state, "step": "failed", "error": "the sign-in page stopped before Google answered; call `connect` again"}
    emit_connect_state(state, as_json)


@cli.command(epilog="\b\nExamples:\n  email mailboxes -a default\n  email mailboxes -a icloud --backend mailapp --json")
@account_option
@backend_option
@json_option
def mailboxes(account: str | None, backend: str, as_json: bool) -> None:
    """List mailboxes (Mail.app, recursive) or labels (Gmail) of an account."""
    name, acc = account_config(account)

    def show(rows: list[dict]) -> None:
        if as_json:
            click.echo(json.dumps(rows, ensure_ascii=False, indent=2))
            return
        for r in rows:
            extra = f"  {r.get('count', '')} msgs, {r.get('unread', '')} unread" if "count" in r else f"  ({r.get('id')})"
            click.echo(f"{r['path']}{extra}")

    run_backend(backend, name, acc,
                gmail=lambda: show([{"path": lb["name"], "id": lb["id"], "type": lb.get("type")} for lb in gmail_labels(gmail_service(name))]),
                mailapp=lambda: show(mailapp_mailboxes(acc["mailapp"])))


@cli.command("list", epilog="\b\nExamples:\n  email list INBOX -a default --since 2026-10-01 --limit 20\n  email list sent -a work --since 2026-10-01 --json")
@click.argument("mailbox", default="INBOX")
@click.option("--since", default=None, help="ISO date/datetime; only messages received at/after it")
@click.option("--limit", default=30, show_default=True)
@account_option
@backend_option
@json_option
def list_cmd(mailbox: str, since: str | None, limit: int, account: str | None, backend: str, as_json: bool) -> None:
    """Recent messages of a mailbox (Mail.app path like "Projects/House") or Gmail label.

    MAILBOX "sent" resolves to the account's own sent folder on either backend.
    """
    name, acc = account_config(account)
    when = parse_since(since)
    q = f"after:{when.strftime('%Y/%m/%d')}" if when else ""
    run_backend(backend, name, acc,
                gmail=lambda: emit(gmail_items(gmail_service(name), name, q, limit, mailbox), as_json),
                mailapp=lambda: emit(mailapp_items(name, acc["mailapp"], mailbox, when, limit), as_json))


@cli.command(epilog="\b\nExamples:\n  email search \"from:alice newer_than:90d\" -a default\n  email search \"invoice\" -a icloud --mailbox INBOX --in body --since 2026-09-01")
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
    when = parse_since(since)

    def via_mailapp() -> None:
        operator = gmail_query_operator(query)
        if operator:
            # Refused rather than run: Mail.app matches it as a literal and returns nothing.
            raise click.ClickException(
                f"{query!r} uses Gmail syntax ({operator}), which Mail.app cannot run. Gmail is unavailable for {name}: "
                f"run `email auth login -a {name}`. Meanwhile use `email list <mailbox> --since <date> -a {name}` "
                f"or a plain substring with --mailbox and --in subject|from|body.")
        if not mailbox:
            raise click.ClickException("Mail.app search needs --mailbox (e.g. INBOX or Projects/House)")
        kwargs = {"subject": query} if field == "subject" else {"sender": query} if field == "from" else {"content": query}
        emit(mailapp_items(name, acc["mailapp"], mailbox, when, limit, **kwargs), as_json)

    if backend == "mailapp" and field == "body" and token_path(name).exists():
        # Gmail indexes bodies server-side; Mail.app would read each one on its main thread.
        log.warning("--in body on %s: searching through the Gmail API instead of Mail.app", name)
        run_backend("auto", name, acc,
                    gmail=lambda: emit(gmail_items(gmail_service(name), name, gmail_body_query(query, mailbox, when), limit, None), as_json),
                    mailapp=via_mailapp)
        return
    q = query + (f" after:{when.strftime('%Y/%m/%d')}" if when else "")
    run_backend(backend, name, acc, gmail=lambda: emit(gmail_items(gmail_service(name), name, q, limit, mailbox), as_json), mailapp=via_mailapp)


@cli.command(epilog="\b\nExamples:\n  email read 18f3a2b9c0d1e2f3 -a default\n  email read \"Google:INBOX:1234\" -a default --json")
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


@cli.command(epilog="\b\nExamples:\n  email attachments 18f3a2b9c0d1e2f3 -a default --out /tmp/attachments")
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


def resolve_body(body: str | None, file_: pathlib.Path | None) -> str:
    if body is not None and file_ is not None:
        raise click.ClickException("--body and --file are mutually exclusive")
    if file_ is not None:
        return reflow_markdown(file_.read_text())
    return body if body is not None else sys.stdin.read()


file_option = click.option("--file", "file_", default=None, type=click.Path(exists=True, dir_okay=False, path_type=pathlib.Path), help="read the body from a Markdown draft file and reflow its hard-wrapped paragraphs into email-normal single lines (mutually exclusive with --body)")


@cli.command(epilog="\b\nExamples:\n  email send alice@example.com --subject \"Hello\" --body \"Hi Alice\" -a default\n  email send alice@example.com --subject \"Quote\" --file draft.md --attach quote.pdf -a default --yes")
@click.argument("to")
@click.option("--subject", required=True)
@click.option("--body", default=None, help="text body; reads stdin when omitted")
@file_option
@click.option("--cc", default=None)
@click.option("--attach", multiple=True, type=click.Path(exists=True, dir_okay=False, path_type=pathlib.Path))
@click.option("--yes", is_flag=True, help="actually send; without it the message is only shown")
@account_option
@backend_option
def send(to: str, subject: str, body: str | None, file_: pathlib.Path | None, cc: str | None, attach: tuple[pathlib.Path, ...], yes: bool, account: str | None, backend: str) -> None:
    """Send a plain-text email from an account. Dry run unless --yes."""
    name, acc = account_config(account)
    backend = resolve_backend(backend, name, acc)
    if backend == "gmail":
        require_gmail_write(name)
    text = resolve_body(body, file_)
    sender = acc.get("address") or ""
    click.echo(f"[{'SEND' if yes else 'dry run'}] via {backend} as {sender}\n  to: {to}\n  cc: {cc or '-'}\n  subject: {subject}\n  attachments: {', '.join(p.name for p in attach) or '-'}\n\n{text}", err=not yes)
    if not yes:
        return
    if backend == "gmail":
        click.echo(f"sent: {gmail_send(gmail_service(name), build_message(sender, to, subject, text, cc, list(attach)))}")
    else:
        mailapp_send(acc["mailapp"], sender, to, subject, text, cc, list(attach))
        click.echo("sent via Mail.app")


@cli.command(epilog="\b\nExamples:\n  email reply 18f3a2b9c0d1e2f3 --file draft.md -a default\n  email reply 18f3a2b9c0d1e2f3 --file draft.md --attach quote.pdf -a default --draft")
@click.argument("msg_id")
@click.option("--body", default=None, help="text body; reads stdin when omitted")
@file_option
@click.option("--to", default=None, help="override recipient (default: original sender)")
@click.option("--cc", default=None, help="override Cc (default: original Cc)")
@click.option("--subject", default=None, help="override subject (default: original subject, 'Re: ' prefixed once)")
@click.option("--attach", multiple=True, type=click.Path(exists=True, dir_okay=False, path_type=pathlib.Path))
@click.option("--yes", is_flag=True, help="actually send; without it the message is only shown")
@click.option("--draft", is_flag=True, help="Gmail only: save the reply as a draft in the thread instead of sending it")
@account_option
@backend_option
def reply(msg_id: str, body: str | None, file_: pathlib.Path | None, to: str | None, cc: str | None, subject: str | None, attach: tuple[pathlib.Path, ...], yes: bool, draft: bool, account: str | None, backend: str) -> None:
    """Reply to msg_id in its existing thread (Gmail: In-Reply-To/References + threadId;
    Mail.app: its own reply). Dry run unless --yes or --draft."""
    name, acc = account_config(account)
    backend = "mailapp" if msg_id.count(":") >= 2 else resolve_backend(backend, name, acc)
    text = resolve_body(body, file_)
    sender = acc.get("address") or ""
    if draft and backend != "gmail":
        raise click.ClickException("--draft needs the gmail backend: Mail.app exposes no scriptable reply draft")
    if draft and yes:
        raise click.ClickException("--draft and --yes are exclusive: a draft is never sent")
    if backend == "gmail":
        require_gmail_write(name)
        svc = gmail_service(name)
        orig = gmail_read(svc, name, msg_id)
        subj = subject or reply_subject(orig["subject"])
        rcpt = to or orig["from"]
        rcc = cc if cc is not None else (orig.get("cc") or None)
        mode = "DRAFT" if draft else "SEND" if yes else "dry run"
        click.echo(f"[{mode}] via gmail as {sender}\n  to: {rcpt}\n  cc: {rcc or '-'}\n  subject: {subj}\n  thread: {orig['thread_id']}\n  attach: {', '.join(a.name for a in attach) or '-'}\n\n{text}", err=not (yes or draft))
        if not (yes or draft):
            return
        msg = build_message(sender, rcpt, subj, text, rcc, list(attach))
        if orig.get("message_id"):
            msg["In-Reply-To"] = orig["message_id"]
            msg["References"] = reply_references(orig.get("references") or "", orig["message_id"])
        if draft:
            click.echo(f"draft: {gmail_draft(svc, msg, thread_id=orig.get('thread_id'))}")
        else:
            click.echo(f"sent: {gmail_send(svc, msg, thread_id=orig.get('thread_id'))}")
    else:
        if to or subject:
            raise click.ClickException("--to/--subject are not supported for the mailapp backend: Mail.app's own `reply` command sets the recipient and subject from the original message")
        orig = mailapp_read(name, msg_id)
        rcc = cc if cc is not None else (orig.get("cc") or None)
        click.echo(f"[{'SEND' if yes else 'dry run'}] via mailapp as {sender}\n  to: {orig['from']}\n  cc: {rcc or '-'}\n  subject: {reply_subject(orig['subject'])}\n\n{text}", err=not yes)
        if not yes:
            return
        acc_name, mailbox, raw_id = msg_id.split(":", 2)
        mailapp_reply(acc_name, mailbox, raw_id, text, rcc, list(attach))
        click.echo("sent via Mail.app")


# --- Gmail drafts ----------------------------------------------------------------

def drafts_service(backend: str, name: str, acc: dict):
    """Drafts live only behind the Gmail API: Mail.app exposes no scriptable draft store."""
    if resolve_backend(backend, name, acc) != "gmail":
        raise click.ClickException(f"draft commands need the gmail backend; account {name!r} has no Gmail token (run `email auth login -a {name}`)")
    return gmail_service(name)


def draft_summary(draft: dict) -> dict:
    """The listing shape of a draft: no body, attachment metadata only."""
    keys = ("draft_id", "message_id", "thread_id", "account", "ts", "from", "to", "cc", "bcc", "subject", "snippet", "in_reply_to")
    out = {k: draft.get(k) for k in keys}
    out["attachments"] = [{"filename": a["filename"], "mimeType": a.get("mimeType"), "size": a.get("size")} for a in draft["attachments"]]
    return out


def fmt_ts(ts: int | None) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else " " * 16


@cli.command(epilog="\b\nExamples:\n  email drafts -a default\n  email drafts -a default -q \"to:alice\" --json")
@click.option("-q", "--query", default=None, help="Gmail query to filter drafts (e.g. 'to:alice subject:devis')")
@click.option("--limit", default=30, show_default=True)
@account_option
@backend_option
@json_option
def drafts(query: str | None, limit: int, account: str | None, backend: str, as_json: bool) -> None:
    """List Gmail drafts: id, thread, recipients, subject, date, attachments."""
    name, acc = account_config(account)
    rows = [draft_summary(d) for d in gmail_drafts(drafts_service(backend, name, acc), name, query, limit)]
    if as_json:
        click.echo(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    for r in rows:
        atts = r["attachments"]
        att = f"  [{len(atts)} att: {', '.join(a['filename'] for a in atts)}]" if atts else ""
        click.echo(f"{fmt_ts(r['ts'])}  {r['draft_id']:<22} thread={r['thread_id'] or '-':<17} {(r['to'] or '-')[:32]:<32} {r['subject'][:60]}{att}")


@cli.command("draft-show", epilog="\b\nExamples:\n  email draft-show r-1234567890 -a default\n  email draft-show r-1234567890 -a default --json")
@click.argument("draft_id")
@account_option
@backend_option
@json_option
def draft_show(draft_id: str, account: str | None, backend: str, as_json: bool) -> None:
    """Print one Gmail draft: headers, plain-text body, attachments."""
    name, acc = account_config(account)
    d = gmail_draft_read(drafts_service(backend, name, acc), name, draft_id)
    if as_json:
        click.echo(json.dumps(d, ensure_ascii=False, indent=2))
        return
    click.echo(f"Draft:   {d['draft_id']} (message {d['message_id']}, thread {d['thread_id']})\nFrom:    {d['from']}\nTo:      {d['to']}\nCc:      {d['cc']}\n"
               f"Bcc:     {d['bcc']}\nDate:    {fmt_ts(d['ts']).strip()}\nSubject: {d['subject']}\nReply-to-message: {d['in_reply_to'] or '-'}\n")
    click.echo(d["body"])
    if d["attachments"]:
        click.echo("\nAttachments: " + ", ".join(f"{a['filename']} ({a.get('size')} B)" for a in d["attachments"]))


@cli.command("draft-update", epilog="\b\nExamples:\n  email draft-update r-1234567890 --file draft.md -a default --keep-attachments\n  email draft-update r-1234567890 --subject \"Re: quote v2\" --attach quote.pdf -a default --yes")
@click.argument("draft_id")
@click.option("--body", default=None, help="new text body (default: keep the draft's current body)")
@file_option
@click.option("--subject", default=None, help="new subject (default: keep)")
@click.option("--to", default=None, help="new To (default: keep)")
@click.option("--cc", default=None, help="new Cc (default: keep; '' clears it)")
@click.option("--attach", multiple=True, type=click.Path(exists=True, dir_okay=False, path_type=pathlib.Path), help="attach a file (repeatable)")
@click.option("--keep-attachments", is_flag=True, help="retain the draft's current attachments; without it they are dropped")
@click.option("--yes", is_flag=True, help="actually update; without it the new draft is only shown")
@account_option
@backend_option
def draft_update(draft_id: str, body: str | None, file_: pathlib.Path | None, subject: str | None, to: str | None, cc: str | None,
                 attach: tuple[pathlib.Path, ...], keep_attachments: bool, yes: bool, account: str | None, backend: str) -> None:
    """Rewrite a Gmail draft in place, keeping its thread and reply headers. Dry run unless --yes."""
    name, acc = account_config(account)
    svc = drafts_service(backend, name, acc)
    require_gmail_write(name)
    d = gmail_draft_read(svc, name, draft_id)
    # The draft's own body is the default, so stdin is never read implicitly here.
    text = resolve_body(body, file_) if body is not None or file_ is not None else d["body"]
    sender = d["from"] or acc.get("address") or ""
    rcpt, subj = to if to is not None else d["to"], subject if subject is not None else d["subject"]
    rcc = (cc if cc is not None else d["cc"]) or None
    kept = d["attachments"] if keep_attachments else []
    dropped = [] if keep_attachments else d["attachments"]
    click.echo(f"[{'UPDATE' if yes else 'dry run'}] draft {draft_id} via gmail as {sender}\n  to: {rcpt or '-'}\n  cc: {rcc or '-'}\n  subject: {subj}\n"
               f"  thread: {d['thread_id']}\n  in-reply-to: {d['in_reply_to'] or '-'}\n"
               f"  attachments kept: {', '.join(a['filename'] for a in kept) or '-'}\n  attachments added: {', '.join(p.name for p in attach) or '-'}\n"
               f"  attachments dropped: {', '.join(a['filename'] for a in dropped) or '-'}\n\n{text}", err=not yes)
    if not yes:
        return
    kept_data = [(a, gmail_attachment_bytes(svc, d["message_id"], a)) for a in kept]
    msg = draft_message(d, sender, rcpt, rcc, subj, text, list(attach), kept_data)
    click.echo(f"draft: {gmail_draft_update(svc, draft_id, msg, d['thread_id'])}")


@cli.command("draft-delete", epilog="\b\nExamples:\n  email draft-delete r-1234567890 r-0987654321 -a default\n  email draft-delete r-1234567890 -a default --yes")
@click.argument("draft_ids", nargs=-1, required=True)
@click.option("--yes", is_flag=True, help="actually delete; without it the drafts are only shown")
@account_option
@backend_option
def draft_delete(draft_ids: tuple[str, ...], yes: bool, account: str | None, backend: str) -> None:
    """Delete Gmail drafts permanently (not moved to trash). Dry run unless --yes."""
    name, acc = account_config(account)
    svc = drafts_service(backend, name, acc)
    require_gmail_write(name)
    for draft_id in draft_ids:
        d = gmail_draft_read(svc, name, draft_id)
        click.echo(f"[{'DELETE' if yes else 'dry run'}] draft {draft_id}  to: {d['to'] or '-'}  subject: {d['subject']}", err=not yes)
        if yes:
            gmail_draft_delete(svc, draft_id)
            click.echo(f"deleted: {draft_id}")


if __name__ == "__main__":
    main()
