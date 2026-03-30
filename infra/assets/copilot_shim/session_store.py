"""
Persistent session store for mapping session keys to Copilot session IDs.

Key format: "{conversation_id}:{user_aad_object_id}"
  - Each user gets their own Copilot session even in group chats.
  - In 1:1 chats, the conversation_id is already unique per user.

Uses Azure Blob Storage for persistence across serverless cold starts.
Falls back to in-memory dict if blob storage is unavailable.
"""

import json
import logging
import os
import time
from typing import Dict, Optional

_CONTAINER_NAME = "copilot-sessions"
_BLOB_NAME = "conversation-sessions.json"

# In-memory cache (fast path within same instance)
_local_cache: Dict[str, dict] = {}

# Blob client singleton
_blob_client = None
_blob_init_attempted = False


def _get_blob_client():
    """Lazy-init the blob client using the Function App's managed identity."""
    global _blob_client, _blob_init_attempted
    if _blob_init_attempted:
        return _blob_client
    _blob_init_attempted = True

    blob_uri = os.environ.get("AzureWebJobsStorage__blobServiceUri")
    client_id = os.environ.get("AzureWebJobsStorage__clientId") or os.environ.get("AZURE_MANAGED_IDENTITY_CLIENT_ID")

    if not blob_uri:
        logging.warning("[SessionStore] No blob URI configured, using in-memory only")
        return None

    try:
        from azure.identity import ManagedIdentityCredential
        from azure.storage.blob import BlobServiceClient, ContainerClient

        credential = ManagedIdentityCredential(client_id=client_id) if client_id else ManagedIdentityCredential()
        service_client = BlobServiceClient(account_url=blob_uri, credential=credential)

        # Ensure container exists
        container_client = service_client.get_container_client(_CONTAINER_NAME)
        try:
            container_client.get_container_properties()
        except Exception:
            try:
                container_client.create_container()
                logging.info(f"[SessionStore] Created container '{_CONTAINER_NAME}'")
            except Exception as ce:
                logging.warning(f"[SessionStore] Could not create container: {ce}")
                return None

        _blob_client = container_client.get_blob_client(_BLOB_NAME)
        logging.info("[SessionStore] Blob storage session store initialized")
        return _blob_client

    except Exception as e:
        logging.warning(f"[SessionStore] Failed to init blob storage: {e}")
        return None


def _load_from_blob() -> Dict[str, dict]:
    """Load the full mapping from blob storage."""
    client = _get_blob_client()
    if not client:
        return {}
    try:
        data = client.download_blob().readall()
        return json.loads(data)
    except Exception:
        # Blob doesn't exist yet or is corrupted
        return {}


def _save_to_blob(mapping: Dict[str, dict]):
    """Save the full mapping to blob storage."""
    client = _get_blob_client()
    if not client:
        return
    try:
        client.upload_blob(
            json.dumps(mapping, indent=2),
            overwrite=True,
            content_settings=None,
        )
    except Exception as e:
        logging.warning(f"[SessionStore] Failed to save to blob: {e}")


def get_session_id(conversation_id: str) -> Optional[str]:
    """Get the Copilot session ID for a Teams conversation.

    Checks in-memory cache first, then blob storage.
    Returns None if no mapping exists.
    """
    if not conversation_id:
        return None

    # Fast path: in-memory cache
    if conversation_id in _local_cache:
        entry = _local_cache[conversation_id]
        return entry.get("session_id")

    # Slow path: load from blob
    mapping = _load_from_blob()
    _local_cache.update(mapping)

    if conversation_id in mapping:
        return mapping[conversation_id].get("session_id")

    return None


def set_session_id(conversation_id: str, session_id: str):
    """Store the mapping from Teams conversation ID to Copilot session ID."""
    if not conversation_id or not session_id:
        return

    entry = {
        "session_id": session_id,
        "updated_at": time.time(),
    }

    _local_cache[conversation_id] = entry

    # Persist to blob (merge with existing)
    mapping = _load_from_blob()
    mapping[conversation_id] = entry

    # Prune old entries (older than 24 hours) to prevent unbounded growth
    cutoff = time.time() - 86400
    mapping = {
        k: v for k, v in mapping.items()
        if v.get("updated_at", 0) > cutoff
    }

    _save_to_blob(mapping)
