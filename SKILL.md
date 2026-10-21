---
name: email-cli
description: Read, search and send the user's email from the terminal via the bundled `email` command, the same commands on every machine. Two backends behind one CLI - the Gmail API with a per-account OAuth token (works from any machine, fast search with Gmail query syntax) and macOS Mail.app over JavaScript for Automation (covers iCloud/Outlook/IMAP accounts, macOS only). Accounts are names (`default` = personal Google, `zama`, `kakarot`, `icloud`, `outlook`); `--backend auto` picks Gmail when a token exists, else Mail.app. Commands - `email accounts`, `email mailboxes`, `email list <mailbox>`, `email search "<query>"`, `email read <id>`, `email attachments <id> --out DIR`, `email send <to> --subject ... --yes`, `email reply <id> --yes` (threaded reply; `--draft` saves it as a Gmail draft in the thread instead), and Gmail drafts - `email drafts [-q QUERY]`, `email draft-show <draft_id>`, `email draft-update <draft_id> --file draft.md --yes` (keeps thread and reply headers), `email draft-delete <draft_id>... --yes`. `send`/`reply` take `--file <draft.md>` instead of `--body` to reflow a hard-wrapped Markdown draft into normal email paragraphs. Every read supports --json. Use when the user wants to read, find, or answer email, save an attachment, or a heartbeat needs a mailbox as data.
---

# email-cli

## How to invoke

`email` is on PATH (linked by claudine's `link_clis.py`); otherwise `bin/email`
from this repo. Every command has `-h`. Reads default to text; add `--json` for
scripts. `send`, `reply`, `draft-update` and `draft-delete` are dry runs unless `--yes`.

```bash
email accounts                                        # names, addresses, which backend each gets here
email mailboxes -a default                            # Gmail labels, or Mail.app mailboxes (recursive, with counts)
email list "Projects/House" -a default --since 2026-04-01 --limit 20
email list sent -a zama --since 2026-10-01             # the account's own sent folder, whatever its locale name
email search "from:alice newer_than:90d" -a default          # Gmail query syntax
email search "Smith" -a default --since 2026-01-01                 # Gmail full-text search
email search "Smith" -a zama --mailbox INBOX --in body --since 2026-09-01   # Mail.app-only account
email read 18f3a2b9c0d1e2f3 -a default               # Gmail id
email read "Google:Projects/House:1234" -a default     # Mail.app id, backend inferred from the shape
email attachments <id> -a default --out ~/Downloads/devis
email send alice@example.com --subject "..." --body "..." -a default --yes
email send alice@example.com --subject "..." --file draft.md -a default --yes    # reflows a hard-wrapped Markdown draft into normal paragraphs before sending
email reply 18f3a2b9c0d1e2f3 --file draft.md -a default --yes        # threaded reply: reuses To/Cc/Subject, sets In-Reply-To/References and Gmail threadId from the original
email reply 18f3a2b9c0d1e2f3 --file draft.md --attach pv.pdf -a default --draft  # same threading, saved as a Gmail draft to review and send from Gmail
email drafts -a default                                # Gmail drafts: date, draft id, thread, To, subject, attachments
email drafts -a default -q "to:alice" --json     # filter with Gmail query syntax
email draft-show r-1234567890 -a default               # headers, body, attachments of one draft
email draft-update r-1234567890 --file draft.md -a default --keep-attachments --yes   # new body, same thread
email draft-update r-1234567890 --subject "Re: devis v2" --attach pv.pdf -a default --yes
email draft-delete r-1234567890 r-0987654321 -a default --yes
```

`--file` is for the case that bit us once: a Markdown draft wrapped at ~78 chars for readability, sent verbatim, lands with line breaks in the middle of sentences. `--file` joins each paragraph back onto one line (headings/list items/blockquotes keep their own line) before sending; `--body`/stdin are sent as-is, so compose those already unwrapped.

`reply` only works from the message being answered, not from a bare `--body`/`--to`: on Gmail it fetches the original's Message-ID/References/threadId and sets them on the outgoing message; on Mail.app it hands off to Mail's own `reply` command, so `--to`/`--subject` are not accepted for that backend (Mail.app already sets them from the original).

## Drafts (Gmail only)

`drafts` lists, `draft-show` prints one, `draft-update` rewrites one in place,
`draft-delete` removes one or more permanently (not via Trash). Draft ids are
Gmail's draft ids (`r-...`), not message ids: take them from `email drafts`
(or from `email reply --draft`, which prints the id it creates).

`draft-update` keeps whatever it is not told to change: To/Cc/Subject/From/Bcc,
the body (when neither `--body` nor `--file` is given; stdin is never read),
and the thread, since the Gmail threadId and the In-Reply-To/References
headers are copied over, so a reply draft stays a reply. Attachments are the
exception: the existing ones are dropped unless `--keep-attachments` is
passed; the dry run lists kept, added and dropped files, so read it first.
The body is rewritten as plain text, so an HTML draft composed in Gmail loses
its formatting once updated. Mail.app accounts get a clear error: Mail.app has
no scriptable draft store.

## Accounts and backends

`~/.config/email-cli/config.json` maps names to an address and the Mail.app
account name. Without a file, the defaults are `default` (alice@example.com,
Mail.app "Google"), `zama`, `kakarot`, `icloud`, `outlook`.

| backend | needs                                          | strengths                                   | limits                                              |
| ------- | ---------------------------------------------- | ------------------------------------------- | --------------------------------------------------- |
| gmail   | `email auth login -a <name>` once, in a browser | any machine, Gmail search, fast, attachments | Google accounts only                                |
| mailapp | macOS with Mail.app and its accounts           | every account Mail.app has                  | Mac only; `--in body` reads each body (refused past 500 candidates, so bound it with `--since` and a mailbox); on an account with a Gmail token it runs through the Gmail API instead |

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
`email accounts` refreshes. **For a Google account, Gmail is the rule, not a
preference**: it is about 30x faster on searches and sees messages Mail.app has
not synced. Do not force `--backend mailapp` on a Google account; if one has no
token yet, run `email auth login -a <name>` once instead. Mail.app is for the
accounts Gmail cannot serve (iCloud, Outlook, IMAP). The Gmail token reuses gdrive-cli's OAuth client
(`client_secret.json`) with the `gmail.modify` and `gmail.compose` scopes; it is
a Document in the 1Password vault `Claudine` so a box restores it without a
browser.

## Dead Gmail token in an unattended run

`stored Gmail grant for <name> is dead` means the token was revoked or expired;
only `email auth login -a <name>` (a browser consent) restores it, so a
heartbeat cannot fix it. Report it to the user as an action item with that
exact command, and keep working through the automatic Mail.app fallback:

- `email list <mailbox> --since <date> -a <name>` and `email list sent ...` work
  as usual.
- `email search` needs `--mailbox` and a plain substring with `--in
  subject|from|body`; Gmail syntax (`after:`, `from:`, `newer_than:`...) is
  refused with an error, since Mail.app would match it literally.
- Mail.app only sees what it has synced, so an empty result is not proof the
  mail does not exist. Say so rather than concluding "nothing received".

Do not hand-roll `osa run 'tell application "Mail" ...'` around the CLI, and do
not pass `--backend mailapp` for this: the fallback already does it.

## Ids

Gmail ids are Gmail's message ids. Mail.app ids are `<account>:<mailbox path>:<id>`
and `read` / `attachments` infer the backend from that shape. Both are stable
for a caller's own state.

## Sent mail

`list sent` / `search --mailbox sent` resolve to each account's real sent
folder, so don't guess its name. On Mail.app it is `Messages envoyés` for
`default` and `kakarot`, `Sent Mail` for `zama`, `Éléments envoyés` for
`outlook`; `default` also has a legacy `Sent Items` that is not the live one.
iCloud exposes no mailboxes to Mail.app here, so `sent` fails there with the
account's top-level list. On Gmail `sent` is the `SENT` label.

## Not this tool

Deciding what an email means. It returns headers, bodies and files; the
judgement belongs to the caller.
