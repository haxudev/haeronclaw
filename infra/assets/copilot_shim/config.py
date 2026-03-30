import logging
import os
from typing import Optional

# Default session state directory used by the Copilot CLI
_DEFAULT_CONFIG_DIR = os.path.expanduser("~/.copilot")
_REMOTE_CONFIG_DIR = "/code-assistant-session"


def resolve_config_dir() -> Optional[str]:
    """
    Resolve the config directory for session state persistence.

    Priority:
    1. CODE_ASSISTANT_CONFIG_PATH env var (explicit override)
    2. CONTAINER_NAME env var is set → /code-assistant-session (remote/Azure Functions mode)
    3. Neither set → None (SDK default ~/.copilot/ is used)
    """
    explicit_path = os.environ.get("CODE_ASSISTANT_CONFIG_PATH")
    if explicit_path:
        logging.info(f"Using CODE_ASSISTANT_CONFIG_PATH: {explicit_path}")
        return explicit_path

    container_name = os.environ.get("CONTAINER_NAME")
    if container_name:
        logging.info(f"Remote mode detected (CONTAINER_NAME={container_name}), using {_REMOTE_CONFIG_DIR}")
        return _REMOTE_CONFIG_DIR

    return None


def session_exists(config_dir: Optional[str], session_id: str) -> bool:
    """
    Check if a session exists on disk by looking for its directory.

    Session state is stored under {config_dir}/session-state/{sessionId}/.
    Falls back to ~/.copilot/session-state/{sessionId}/ if config_dir is None.
    """
    base = config_dir if config_dir else _DEFAULT_CONFIG_DIR
    session_path = os.path.join(base, "session-state", session_id)
    exists = os.path.isdir(session_path)
    logging.info(f"Session '{session_id}' exists at {session_path}: {exists}")
    return exists
