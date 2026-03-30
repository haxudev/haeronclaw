"""
Restore m365-cli credentials at runtime.

On Azure Container Apps, the local file system doesn't persist ~/.m365-cli/credentials.json.
This module restores credentials from the M365_CLI_CREDENTIALS env var, which is backed
by a Container App encrypted secret (secretref). Supports raw JSON or base64-encoded JSON.
"""

import json
import logging
import os


_M365_DIR = os.path.expanduser("~/.m365-cli")
_CRED_FILE = os.path.join(_M365_DIR, "credentials.json")

_restored = False


def _write_credentials(cred_json: str, source: str) -> bool:
    """Validate and write credentials JSON to disk."""
    json.loads(cred_json)  # validate
    os.makedirs(_M365_DIR, mode=0o700, exist_ok=True)
    with open(_CRED_FILE, "w", encoding="utf-8") as f:
        f.write(cred_json)
    os.chmod(_CRED_FILE, 0o600)
    logging.info("[m365-creds] Restored credentials from %s to %s", source, _CRED_FILE)
    return True


def restore_m365_credentials() -> bool:
    """Restore m365-cli credentials to ~/.m365-cli/credentials.json.

    Returns True if credentials were successfully restored, False otherwise.
    Skips silently if credentials already exist on disk or no source is configured.
    """
    global _restored
    if _restored:
        return True

    if os.path.isfile(_CRED_FILE):
        logging.info("[m365-creds] Credentials already exist at %s, skipping restore", _CRED_FILE)
        _restored = True
        return True

    env_creds = (os.environ.get("M365_CLI_CREDENTIALS") or "").strip()
    if env_creds:
        try:
            _restored = _write_credentials(env_creds, "M365_CLI_CREDENTIALS env var")
            return _restored
        except (json.JSONDecodeError, ValueError):
            pass
        try:
            import base64
            decoded = base64.b64decode(env_creds).decode("utf-8")
            _restored = _write_credentials(decoded, "M365_CLI_CREDENTIALS env var (base64)")
            return _restored
        except Exception as exc:
            logging.warning("[m365-creds] M365_CLI_CREDENTIALS env var invalid: %s", exc)

    logging.info("[m365-creds] No credential source configured (M365_CLI_CREDENTIALS env var not set)")
    return False
