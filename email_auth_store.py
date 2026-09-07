"""Keep OAuth working copies subordinate to the shared credential broker."""

import json
import os
from pathlib import Path
import subprocess
import tempfile


def broker(action, connector, account, document=None):
    """Keep credential values on stdin and suppress provider error bodies."""
    command = ["claudine-secret", "auth", action, connector, "--account", account]
    if action == "status":
        command.append("--json")
    try:
        result = subprocess.run(command, input=json.dumps(document) if document is not None else None,
                                capture_output=True, text=True, timeout=35)
        if result.returncode not in ((0,) if action == "load" else (0, 3)):
            return None
        value = json.loads(result.stdout)
        return value if isinstance(value, dict) else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def write_private(path, document):
    """Atomic private files prevent partial token reads during refresh."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".credential-")
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(document, handle)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def marker(path, suffix):
    return Path(str(path) + suffix)


def restore(path, connector, account):
    """Unsynced refreshes and explicit local logout take priority over vault reads."""
    if path.exists():
        path.chmod(0o600)
    if marker(path, ".signed-out").exists() or marker(path, ".pending").exists():
        return path.exists()
    document = broker("load", connector, account)
    if document:
        write_private(path, document)
    return path.exists()


def save(path, document, connector, account):
    """Persist first so an unavailable broker cannot lose refreshed credentials."""
    write_private(marker(path, ".pending"), {"pending": True})
    write_private(path, document)
    marker(path, ".signed-out").unlink(missing_ok=True)
    result = broker("save", connector, account, document)
    if result and result.get("source") in ("vault", "pending"):
        marker(path, ".pending").unlink(missing_ok=True)
    return result


def status(path, connector, account):
    """Only authentication metadata is exposed to callers."""
    if marker(path, ".signed-out").exists():
        return dict(connector=connector, account=account, source="unavailable",
                    configured=False, pending=False, last_sync=None)
    result = broker("status", connector, account) or {}
    fields = ("connector", "account", "source", "configured", "pending", "last_sync")
    result = {key: result.get(key) for key in fields}
    result.update(connector=connector, account=account)
    if marker(path, ".pending").exists() or (path.exists() and not result.get("configured")):
        result.update(source="local", configured=True, pending=True, last_sync=None)
    else:
        result.update(source=result.get("source") or "unavailable",
                      configured=bool(result.get("configured")), pending=bool(result.get("pending")))
    return result


def sync(path, connector, account):
    """Import existing working copies once, then flush the broker's pending value."""
    if marker(path, ".signed-out").exists():
        return status(path, connector, account)
    if marker(path, ".pending").exists():
        save(path, json.loads(path.read_text()), connector, account)
    else:
        document = broker("load", connector, account)
        if document:
            write_private(path, document)
            broker("sync", connector, account)
        elif path.exists():
            save(path, json.loads(path.read_text()), connector, account)
    return status(path, connector, account)
