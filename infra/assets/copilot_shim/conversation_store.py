"""Durable conversation history store for Teams bot.

The Copilot SDK session_id alone is not sufficient to guarantee multi-turn context
across Azure Functions cold starts. This module persists a short
rolling window of user/assistant turns to Azure Blob Storage and allows rendering
that history into a prompt prefix.

Storage:
  - Container: copilot-sessions
  - Blob per conversation key: histories/{sha256(key)}.json
"""

import hashlib
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional


_CONTAINER_NAME = "copilot-sessions"
_BLOB_PREFIX = "histories"

_DEFAULT_MAX_TURNS = 12  # user+assistant pairs
_DEFAULT_MAX_CHARS_PER_MESSAGE = 2000

_service_client = None


def _get_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except Exception:
        return default


def _get_blob_service_client():
    global _service_client
    if _service_client is not None:
        return _service_client

    blob_uri = os.environ.get("AzureWebJobsStorage__blobServiceUri")
    client_id = os.environ.get("AzureWebJobsStorage__clientId") or os.environ.get(
        "AZURE_MANAGED_IDENTITY_CLIENT_ID"
    )

    if not blob_uri:
        logging.warning("[ConversationStore] AzureWebJobsStorage__blobServiceUri not set")
        return None

    try:
        from azure.identity import ManagedIdentityCredential
        from azure.storage.blob import BlobServiceClient

        credential = (
            ManagedIdentityCredential(client_id=client_id)
            if client_id
            else ManagedIdentityCredential()
        )
        _service_client = BlobServiceClient(account_url=blob_uri, credential=credential)
        return _service_client
    except Exception as exc:
        logging.warning(f"[ConversationStore] Failed to init BlobServiceClient: {exc}")
        return None


def _ensure_container(service_client) -> bool:
    try:
        container = service_client.get_container_client(_CONTAINER_NAME)
        try:
            container.get_container_properties()
        except Exception:
            container.create_container()
            logging.info(f"[ConversationStore] Created container '{_CONTAINER_NAME}'")
        return True
    except Exception as exc:
        logging.warning(f"[ConversationStore] Failed to ensure container: {exc}")
        return False


def _blob_name_for_key(key: str) -> str:
    digest = hashlib.sha256(key.encode("utf-8", errors="ignore")).hexdigest()
    return f"{_BLOB_PREFIX}/{digest}.json"


def _load_blob_json(blob_client) -> Dict[str, Any]:
    try:
        raw = blob_client.download_blob().readall()
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def _save_blob_json(blob_client, data: Dict[str, Any]) -> None:
    try:
        blob_client.upload_blob(
            json.dumps(data, ensure_ascii=False, indent=2),
            overwrite=True,
        )
    except Exception as exc:
        logging.warning(f"[ConversationStore] Failed to save history: {exc}")


def _trim_text(text: str, limit: int) -> str:
    value = (text or "").strip()
    if len(value) <= limit:
        return value
    return value[:limit] + "..."


def load_history(key: str) -> List[Dict[str, Any]]:
    if not key:
        return []

    service = _get_blob_service_client()
    if not service:
        return []

    if not _ensure_container(service):
        return []

    blob_client = service.get_blob_client(_CONTAINER_NAME, _blob_name_for_key(key))
    doc = _load_blob_json(blob_client)
    messages = doc.get("messages")
    if isinstance(messages, list):
        return [m for m in messages if isinstance(m, dict)]
    return []


def append_turn(key: str, user_text: str, assistant_text: str) -> None:
    if not key:
        return

    service = _get_blob_service_client()
    if not service:
        return

    if not _ensure_container(service):
        return

    max_turns = _get_int_env("CONVERSATION_HISTORY_TURNS", _DEFAULT_MAX_TURNS)
    max_chars = _get_int_env("CONVERSATION_HISTORY_MAX_CHARS", _DEFAULT_MAX_CHARS_PER_MESSAGE)
    keep_messages = max(2, int(max_turns) * 2)

    blob_client = service.get_blob_client(_CONTAINER_NAME, _blob_name_for_key(key))
    doc = _load_blob_json(blob_client)
    messages = doc.get("messages")
    if not isinstance(messages, list):
        messages = []

    now = time.time()
    messages.append({"role": "user", "text": _trim_text(user_text, max_chars), "ts": now})
    messages.append(
        {"role": "assistant", "text": _trim_text(assistant_text, max_chars), "ts": now}
    )

    if len(messages) > keep_messages:
        messages = messages[-keep_messages:]

    doc = {"key": key, "updated_at": now, "messages": messages}
    _save_blob_json(blob_client, doc)


def render_history_for_prompt(key: str) -> str:
    messages = load_history(key)
    if not messages:
        return ""

    max_turns = _get_int_env("CONVERSATION_HISTORY_TURNS", _DEFAULT_MAX_TURNS)
    max_messages = max(2, int(max_turns) * 2)
    messages = messages[-max_messages:]

    lines: List[str] = []
    for msg in messages:
        role = (msg.get("role") or "").strip().lower()
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        if role == "user":
            lines.append(f"User: {text}")
        elif role == "assistant":
            lines.append(f"Assistant: {text}")

    return "\n".join(lines).strip()
