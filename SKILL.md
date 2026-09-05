---
name: email-cli
description: Read, search and send the user's email from the terminal via the bundled `email` command, the same commands on every machine. Two backends behind one CLI - the Gmail API with a per-account OAuth token (works from any machine, fast search with Gmail query syntax) and macOS Mail.app over JavaScript for Automation (covers iCloud/Outlook/IMAP accounts, macOS only). Accounts are names (`default` = personal Google, `zama`, `kakarot`, `icloud`, `outlook`); `--backend auto` picks Gmail when a token exists, else Mail.app. Commands - `email accounts`, `email mailboxes`, `email list <mailbox>`, `email search "<query>"`, `email read <id>`, `email attachments <id> --out DIR`, `email send <to> --subject ... --yes`, `email reply <id> --yes` (threaded reply). `send`/`reply` take `--file <draft.md>` instead of `--body` to reflow a hard-wrapped Markdown draft into normal email paragraphs. Every read supports --json. Use when the user wants to read, find, or answer email, save an attachment, or a heartbeat needs a mailbox as data.
---

# email-cli

## How to invoke

`email` is on PATH (linked by claudine's `link_clis.py`); otherwise `bin/email`
from this repo. Every command has `-h`. Reads default to text; add `--json` for
scripts. `send` is a dry run unless `--yes`.

```bash
email accounts                                        # names, addresses, which backend each gets here
email mailboxes -a default                            # Gmail labels, or Mail.app mailboxes (recursive, with counts)
email list "Immo/Poncelet" -a default --since 2026-04-01 --limit 20
email search "from:loic.thomas newer_than:90d" -a default          # Gmail query syntax
email search "Bouny" -a default --backend mailapp --mailbox Immo/Poncelet --in body --since 2026-01-01
email read 18f3a2b9c0d1e2f3 -a default               # Gmail id
email read "Google:Immo/Poncelet:1234" -a default     # Mail.app id, backend inferred from the shape
email attachments <id> -a default --out ~/Downloads/devis
email send loic.thomas@lescallier.fr --subject "..." --body "..." -a default --yes
email send loic.thomas@lescallier.fr --subject "..." --file draft.md -a default --yes    # reflows a hard-wrapped Markdown draft into normal paragraphs before sending
email reply 18f3a2b9c0d1e2f3 --file draft.md -a default --yes        # threaded reply: reuses To/Cc/Subject, sets In-Reply-To/References and Gmail threadId from the original
```

`--file` is for the case that bit us once: a Markdown draft wrapped at ~78 chars for readability, sent verbatim, lands with line breaks in the middle of sentences. `--file` joins each paragraph back onto one line (headings/list items/blockquotes keep their own line) before sending; `--body`/stdin are sent as-is, so compose those already unwrapped.

`reply` only works from the message being answered, not from a bare `--body`/`--to`: on Gmail it fetches the original's Message-ID/References/threadId and sets them on the outgoing message; on Mail.app it hands off to Mail's own `reply` command, so `--to`/`--subject` are not accepted for that backend (Mail.app already sets them from the original).

## Accounts and backends

`~/.config/email-cli/config.json` maps names to an address and the Mail.app
account name. Without a file, the defaults are `default` (clement0walter@gmail.com,
Mail.app "Google"), `zama`, `kakarot`, `icloud`, `outlook`.

| backend | needs                                          | strengths                                   | limits                                              |
| ------- | ---------------------------------------------- | ------------------------------------------- | --------------------------------------------------- |
| gmail   | `email auth login -a <name>` once, in a browser | any machine, Gmail search, fast, attachments | Google accounts only                                |
| mailapp | macOS with Mail.app and its accounts           | every account Mail.app has                  | Mac only; `--in body` search is slow, bound it with `--since` and a mailbox |

`auto` = gmail when `~/.config/email-cli/accounts/<name>/token.json` exists,
else mailapp on macOS. **For a Google account, Gmail is the rule, not a
preference**: it is about 30x faster on searches and sees messages Mail.app has
not synced. Do not force `--backend mailapp` on a Google account; if one has no
token yet, run `email auth login -a <name>` once instead. Mail.app is for the
accounts Gmail cannot serve (iCloud, Outlook, IMAP). The Gmail token reuses gdrive-cli's OAuth client
(`client_secret.json`) with the `gmail.modify` and `gmail.compose` scopes; it is
a Document in the 1Password vault `Claudine` so a box restores it without a
browser.

## Ids

Gmail ids are Gmail's message ids. Mail.app ids are `<account>:<mailbox path>:<id>`
and `read` / `attachments` infer the backend from that shape. Both are stable
for a caller's own state.

## Not this tool

Deciding what an email means. It returns headers, bodies and files; the
judgement belongs to the caller.
