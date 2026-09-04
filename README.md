# email-cli

One `email` command for every machine: Gmail API where an OAuth token exists,
macOS Mail.app (JavaScript for Automation) where a Mac is. See `SKILL.md`.

```bash
bin/email accounts
bin/email search "from:syndic newer_than:30d" -a default --json
PYTHONPATH=. uv run --with pytest --with click --with google-api-python-client --with google-auth-oauthlib pytest -q tests
```
