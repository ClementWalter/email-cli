# email-cli

**Visibility: public.**

One `email` command for every machine: Gmail API where an OAuth token exists,
macOS Mail.app (JavaScript for Automation) where a Mac is. See `SKILL.md`.

```bash
bin/email accounts
bin/email search "from:alice newer_than:30d" -a default --json
```

Put `email` on `$PATH` with `uv run claudine/scripts/link_clis.py`.

## Tests

Each test file is a PEP 723 script with pinned dependencies, offline:

```bash
uv run --script tests/test_email_cli.py
uv run --script tests/test_drafts.py
uv run --script tests/test_access.py
uv run --script tests/test_auth_store.py
```

## Credential synchronization

The `claudine-secret` broker is the credential authority. Google OAuth tokens
remain private (0600), atomic SDK working copies. Credentials load from the
broker before Google API use; login and token refresh save back through it. An
unavailable broker preserves the working copy and marks unsynced refreshes so a
stale vault value cannot replace them. Existing local credentials continue to
work and can be imported explicitly.

`auth-status --account default --json` reports metadata without credentials.
`auth-sync --account default --json` imports an existing account or retries
pending synchronization; exit 3 means unavailable or still pending. Both
commands also exist as `auth status` and `auth sync`. Source `local` means the
SDK copy exists but vault synchronization is unverified. Connect an account with
`email auth login --account <name>`.

## Without a terminal

`email google-setup` prints the one-time Google Cloud setup
([GOOGLE_SETUP.md](GOOGLE_SETUP.md)): the user's own project with the Gmail API
and a Desktop app OAuth client, shared with gdrive-cli. `email connect` signs an
account in through the browser from a detached helper and returns at once;
`email connect-status` reports the outcome. `email connect --mailapp <name>`
uses a Mail.app account instead. `mcpb.json` packs it as the "Email" Claude
Desktop extension (`mcp-bridge pack email-cli --version X.Y.Z`), whose settings
feed `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` or
`GOOGLE_CLIENT_FILE`.
