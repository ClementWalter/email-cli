---
name: email-cli
description: Read, search and send the user's email from the terminal via the bundled `email` command, the same commands on every machine. Two backends behind one CLI - the Gmail API with a per-account OAuth token (works from any machine, fast search with Gmail query syntax) and macOS Mail.app over JavaScript for Automation (covers iCloud/Outlook/IMAP accounts, macOS only). Accounts are names mapped in ~/.config/email-cli/config.json (e.g. `default`, `work`, `icloud`); `--backend auto` picks Gmail when a token exists, else Mail.app. Commands - `email accounts`, `email mailboxes`, `email list <mailbox>`, `email search "<query>"`, `email read <id>`, `email attachments <id> --out DIR`, `email send <to> --subject ... --yes`, `email reply <id> --yes` (threaded reply; `--draft` saves it as a Gmail draft in the thread instead), and Gmail drafts - `email drafts [-q QUERY]`, `email draft-show <draft_id>`, `email draft-update <draft_id> --file draft.md --yes` (keeps thread and reply headers), `email draft-delete <draft_id>... --yes`. `send`/`reply` take `--file <draft.md>` instead of `--body` to reflow a hard-wrapped Markdown draft into normal email paragraphs. Every read supports --json. Use when the user wants to read, find, or answer email, save an attachment, or a heartbeat needs a mailbox as data.
---

# email-cli

## How to invoke

`email` is on `$PATH`, linked into `~/.local/bin` by
`uv run claudine/scripts/link_clis.py`; otherwise run `<skill-dir>/bin/email`
from this repo. Every command has `-h` with examples.

## Authentication

Accounts live in `~/.config/email-cli/config.json`:

```json
{"default_account": "default",
 "accounts": {"default": {"address": "alice@example.com", "mailapp": "Google"}}}
```

`default_account` is used when `-a` is omitted; `mailapp` is the account's name
in Mail.app. Without that file no account is known and every command stops with
`unknown account`.

- **Gmail backend:** `email auth login -a <name>` once, in a browser. It asks
  for an access level (or takes `--access read|write`): `read` grants
  `gmail.readonly` only, so Google refuses any send or draft write and `send`,
  `reply` and `draft-update`/`draft-delete` stop with a "read-only" error;
  `write` grants `gmail.modify` and `gmail.compose`. `email accounts` shows each
  token's level. The token is saved under
  `~/.config/email-cli/accounts/<name>/token.json` and reuses gdrive-cli's OAuth
  client (`client_secret.json`).
- **Vault:** the token is a working copy of the `claudine-secret` broker's
  credential (1Password vault), so a box restores it without a browser.
  `email auth-status -a <name> --json` reports source and pending sync without
  secrets; `email auth-sync -a <name> --json` imports a local token or retries a
  pending sync (exit 3 while still pending). Both also exist as `auth status` /
  `auth sync`. Source `local` means the token exists but its vault sync is
  unverified.
- **Mail.app backend:** no auth of its own; it uses the accounts Mail.app has.
- **Without a terminal (Claude Desktop extension):** `email google-setup` prints
  the one-time Google Cloud steps (own project, Gmail API, consent screen,
  Desktop app OAuth client) for Claude to walk the user through in Chrome, plus
  what is configured here. The OAuth client comes from `GOOGLE_OAUTH_CLIENT_ID`
  + `GOOGLE_OAUTH_CLIENT_SECRET` or `GOOGLE_OAUTH_CLIENT_FILE` (the extension's
  settings), `--client-file`, `~/.config/email-cli/client_secret.json`,
  gdrive-cli's, or the newest `client_secret_*.json` in Downloads (copied
  privately on `connect`). `email connect [-a NAME] [--access read|write]`
  opens Google's sign-in page from a detached helper and returns at once (the
  page waits 10 minutes); `email connect-status` tells waiting / done / failed.
  It saves the token like `auth login` and adds the account (with its address)
  to config.json, the first one becoming the default. `email connect -a icloud
  --mailapp iCloud` registers a Mail.app account instead (macOS).

## Output conventions

Reads print compact text, one line per message, by default; `--json` gives
structured records for scripts. Diagnostics, warnings and backend fallbacks go
to stderr (`-v` for INFO logs), so `email … --json | jq` always parses. `send`,
`reply`, `draft-update` and `draft-delete` are dry runs that print what would be
sent unless `--yes`.

Ids: Gmail ids are Gmail's message ids. Mail.app ids are
`<account>:<mailbox path>:<id>` and `read` / `attachments` infer the backend
from that shape. Both are stable for a caller's own state. Draft ids are Gmail's
draft ids (`r-...`), not message ids.

## Commands

```bash
# Read
email accounts                                        # names, addresses, which backend each gets here
email mailboxes -a default                            # Gmail labels, or Mail.app mailboxes (recursive, with counts)
email list "Projects/House" -a default --since 2026-04-01 --limit 20
email list sent -a work --since 2026-10-01            # the account's own sent folder, whatever its locale name
email search "from:alice newer_than:90d" -a default   # Gmail query syntax
email search "invoice" -a default --since 2026-01-01  # Gmail full-text search
email search "invoice" -a icloud --mailbox INBOX --in body --since 2026-09-01   # Mail.app-only account
email read 18f3a2b9c0d1e2f3 -a default                # Gmail id
email read "Google:Projects/House:1234" -a default    # Mail.app id, backend inferred from the shape
email attachments 18f3a2b9c0d1e2f3 -a default --out ~/Downloads/quotes
email drafts -a default                               # Gmail drafts: date, draft id, thread, To, subject, attachments
email drafts -a default -q "to:alice" --json          # filter with Gmail query syntax
email draft-show r-1234567890 -a default              # headers, body, attachments of one draft

# Write (dry run unless --yes)
email send alice@example.com --subject "..." --body "..." -a default --yes
email send alice@example.com --subject "..." --file draft.md -a default --yes   # reflows a hard-wrapped Markdown draft
email reply 18f3a2b9c0d1e2f3 --file draft.md -a default --yes                   # threaded reply
email reply 18f3a2b9c0d1e2f3 --file draft.md --attach pv.pdf -a default --draft # saved as a Gmail draft in the thread
email draft-update r-1234567890 --file draft.md -a default --keep-attachments --yes   # new body, same thread
email draft-update r-1234567890 --subject "Re: quote v2" --attach pv.pdf -a default --yes
email draft-delete r-1234567890 r-0987654321 -a default --yes
email auth login -a default --access read
```

## Recipes

```bash
# What did Alice send me this quarter?
email search "from:alice after:2026/07/01" -a default --json | jq '.[] | {id, subject, ts}'

# Save the attachments of the latest matching quote
id=$(email search "subject:quote has:attachment" -a default --limit 1 --json | jq -r '.[0].id')
email attachments "$id" -a default --out ~/Downloads/quotes

# Answer a thread from a Markdown draft, reviewed in Gmail before it leaves
email reply "$id" --file draft.md -a default --draft

# What did I send from the work account this week?
email list sent -a work --since 2026-10-20
```

## Gotchas

**`--file` vs `--body`.** A Markdown draft wrapped at ~78 chars for
readability, sent verbatim, lands with line breaks in the middle of sentences.
`--file` joins each paragraph back onto one line (headings/list items/
blockquotes keep their own line) before sending; `--body`/stdin are sent as-is,
so compose those already unwrapped.

**`reply` needs the original.** On Gmail it fetches the original's
Message-ID/References/threadId and sets them on the outgoing message; on
Mail.app it hands off to Mail's own `reply` command, so `--to`/`--subject` are
not accepted for that backend (Mail.app already sets them from the original).

**Drafts are Gmail only.** `drafts` lists, `draft-show` prints one,
`draft-update` rewrites one in place, `draft-delete` removes one or more
permanently (not via Trash). Take draft ids from `email drafts` (or from
`email reply --draft`, which prints the id it creates). `draft-update` keeps
whatever it is not told to change: To/Cc/Subject/From/Bcc, the body (when
neither `--body` nor `--file` is given; stdin is never read), and the thread,
since the Gmail threadId and the In-Reply-To/References headers are copied
over, so a reply draft stays a reply. Attachments are the exception: the
existing ones are dropped unless `--keep-attachments` is passed; the dry run
lists kept, added and dropped files, so read it first. The body is rewritten as
plain text, so an HTML draft composed in Gmail loses its formatting once
updated. Mail.app accounts get a clear error: Mail.app has no scriptable draft
store.

**Backends.**

| backend | needs                                           | strengths                                    | limits                                                                                                                                                                                   |
| ------- | ----------------------------------------------- | -------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| gmail   | `email auth login -a <name>` once, in a browser | any machine, Gmail search, fast, attachments | Google accounts only                                                                                                                                                                     |
| mailapp | macOS with Mail.app and its accounts            | every account Mail.app has                   | Mac only; `--in body` reads each body (refused past 500 candidates, so bound it with `--since` and a mailbox); on an account with a Gmail token it runs through the Gmail API instead |

`auto` = gmail when `~/.config/email-cli/accounts/<name>/token.json` exists,
else mailapp on macOS. Mail.app is the fallback, not a peer: under `auto`,
`mailboxes`/`list`/`search` switch to it (with a warning) when Gmail times out
after 20 s, is unreachable, answers 429/5xx or has a dead token. `read`,
`attachments` and `reply` cannot switch (ids are backend-specific), and `send`
never does, since a timed-out Gmail send may still have been delivered. An
explicit `--backend` never switches. A Mail.app account syncing from
`imap.gmail.com` is a Google account: `email accounts` flags it (and `auto`
warns) while it has no token, so connect it with `email auth login -a <name>`.
The IMAP hosts are cached in `~/.config/email-cli/mailapp-servers.json`, which
`email accounts` refreshes.

**For a Google account, Gmail is the rule, not a preference**: it is about 30x
faster on searches and sees messages Mail.app has not synced. Do not force
`--backend mailapp` on a Google account; if one has no token yet, run
`email auth login -a <name>` once instead. Mail.app is for the accounts Gmail
cannot serve (iCloud, Outlook, IMAP).

**Dead Gmail token in an unattended run.** `stored Gmail grant for <name> is
dead` means the token was revoked or expired; only `email auth login -a <name>`
(a browser consent) restores it, so a heartbeat cannot fix it. Report it to the
user as an action item with that exact command, and keep working through the
automatic Mail.app fallback:

- `email list <mailbox> --since <date> -a <name>` and `email list sent ...` work
  as usual.
- `email search` needs `--mailbox` and a plain substring with
  `--in subject|from|body`; Gmail syntax (`after:`, `from:`, `newer_than:`...)
  is refused with an error, since Mail.app would match it literally.
- Mail.app only sees what it has synced, so an empty result is not proof the
  mail does not exist. Say so rather than concluding "nothing received".

Do not hand-roll `osa run 'tell application "Mail" ...'` around the CLI, and do
not pass `--backend mailapp` for this: the fallback already does it.

**Sent mail.** `list sent` / `search --mailbox sent` resolve to each account's
real sent folder, whatever its locale name (`Messages envoyés`, `Sent Mail`,
`Éléments envoyés`...), so don't guess it. An account can also carry a legacy
`Sent Items` that is not the live one. An iCloud account that exposes no
mailboxes to Mail.app makes `sent` fail with the account's top-level list. On
Gmail `sent` is the `SENT` label.

## Not this tool

Deciding what an email means. It returns headers, bodies and files; the
judgement belongs to the caller.
