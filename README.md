# email-cli

One `email` command for every machine: Gmail API where an OAuth token exists,
macOS Mail.app (JavaScript for Automation) where a Mac is. See `SKILL.md`.

```bash
bin/email accounts
bin/email search "from:syndic newer_than:30d" -a default --json
PYTHONPATH=. uv run --with pytest --with click --with google-api-python-client --with google-auth-oauthlib pytest -q tests
```
# Credential synchronization

Brain's `claudine-secret` broker is the credential authority. Google OAuth
tokens remain private (0600), atomic SDK working copies. Credentials load from
the broker before Google API use; login and token refresh save back through it.
An unavailable broker preserves the working copy and marks unsynced refreshes
so a stale vault value cannot replace them. Existing local credentials continue
to work and can be imported explicitly.

`auth-status --account default --json` reports metadata without credentials.
`auth-sync --account default --json` imports an existing account or retries
pending synchronization; exit 3 means unavailable or still pending. Both commands
also exist as `auth status` and `auth sync`. Source `local` means the SDK copy
exists but vault synchronization is unverified. Native setup calls these commands
internally; users connect through Brain's login screens.
