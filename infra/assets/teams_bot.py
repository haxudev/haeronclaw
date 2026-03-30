"""
Microsoft Teams Bot integration via Bot Framework SDK.

Handles incoming messages from Teams, routes them through the Copilot SDK agent,
and sends responses back. Uses User-Assigned Managed Identity for authentication
(no app secret required).

Uses proactive messaging pattern: the Bot Framework callback returns HTTP 200
immediately (within seconds) and heavy agent processing runs in a background
asyncio task.  Results are delivered via ``ADAPTER.continue_conversation()``.
This prevents the Bot Framework Service 15-second gateway timeout from
silently dropping the first message after an idle period.

Supports:
  - Text messages (original)
  - Voice messages / audio attachments (STT transcription via Azure Speech Service)

Architecture:
  CloudAdapterBase + BotFrameworkAuthenticationFactory
    + ManagedIdentityServiceClientCredentialsFactory
  This is the correct approach for UserAssignedMSI bots (no app password).
"""

import asyncio
import json
import logging
import os
import time
import traceback
from http import HTTPStatus
from pathlib import Path
from typing import Optional

import aiohttp
from botbuilder.core import BotAdapter, CloudAdapterBase, TurnContext
from botbuilder.schema import (
    ActionTypes,
    Activity,
    ActivityTypes,
    Attachment,
    CardAction,
    ConversationReference,
    HeroCard,
)
from botframework.connector.auth import (
    AuthenticationConfiguration,
    BotFrameworkAuthenticationFactory,
    ManagedIdentityServiceClientCredentialsFactory,
)

from copilot_shim import run_copilot_agent
from copilot_shim.session_store import get_session_id, set_session_id
from copilot_shim.conversation_store import append_turn, render_history_for_prompt
from model_identity import build_runtime_model_response, is_model_identity_question

# ------------------------------------------------------------------
# Adapter with Managed Identity authentication
# ------------------------------------------------------------------
_BOT_APP_ID = os.environ.get(
    "BOT_APP_ID",
    os.environ.get("AZURE_MANAGED_IDENTITY_CLIENT_ID", ""),
)

_credential_factory = ManagedIdentityServiceClientCredentialsFactory(
    app_id=_BOT_APP_ID,
)

_auth = BotFrameworkAuthenticationFactory.create(
    credential_factory=_credential_factory,
    auth_configuration=AuthenticationConfiguration(),
)

ADAPTER = CloudAdapterBase(_auth)


def _is_true(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _is_production_environment() -> bool:
    """Best-effort production/cloud environment detection.

    Returns True when running on Azure App Service, Azure Functions,
    or Azure Container Apps — unless an explicit non-production APP_ENV is set.
    """
    if (os.environ.get("AZURE_FUNCTIONS_ENVIRONMENT") or "").strip().lower() == "production":
        return True

    app_env = (
        os.environ.get("APP_ENV")
        or os.environ.get("ENVIRONMENT")
        or os.environ.get("ENV")
        or ""
    ).strip().lower()
    if app_env in {"prod", "production"}:
        return True

    # WEBSITE_SITE_NAME is present on Azure App Service / Functions runtime.
    if os.environ.get("WEBSITE_SITE_NAME") and app_env == "":
        return True

    # CONTAINER_APP_NAME is injected by Azure Container Apps platform.
    # Treat all ACA deployments as production-like (override with
    # TEAMS_SEND_WELCOME_MESSAGE=true for dev ACA environments if needed).
    if os.environ.get("CONTAINER_APP_NAME") and app_env == "":
        return True

    return False


def _should_send_welcome_message() -> bool:
    """Control conversation-update welcome message behavior.

    Override with TEAMS_SEND_WELCOME_MESSAGE=true/false when needed.
    Default: disabled in production, enabled in non-production.
    """
    override = os.environ.get("TEAMS_SEND_WELCOME_MESSAGE")
    if override is not None:
        return _is_true(override)
    return not _is_production_environment()


# ------------------------------------------------------------------
# Error handler
# ------------------------------------------------------------------
async def _on_error(context: TurnContext, error: Exception):
    logging.error(f"[Teams Bot] unhandled error: {error}", exc_info=True)
    try:
        await context.send_activity("Sorry, an internal error occurred. Please try again.")
    except Exception:
        logging.error("[Teams Bot] failed to send error message to user", exc_info=True)


ADAPTER.on_turn_error = _on_error

# ------------------------------------------------------------------
# Session tracking
# ------------------------------------------------------------------
# Key format: "{conversation_id}:{user_id}" — each user gets their own
# independent Copilot session, even inside group chats.  This prevents
# User A's context from leaking into User B's responses.
# Persistent via Azure Blob Storage (see copilot_shim/session_store.py).


def _session_keys(turn_context: TurnContext) -> list[str]:
    """Build candidate per-user/per-conversation keys.

    Teams payloads may not consistently include the same user identifier
    field across turns (for example, aad_object_id can be present on one
    message and absent on the next). To avoid context loss, derive multiple
    alias keys and use read fallback + write fan-out.
    """
    conv = turn_context.activity.conversation
    sender = turn_context.activity.from_property
    if not conv or not sender or not getattr(conv, "id", None):
        return []

    channel_data = turn_context.activity.channel_data or {}
    channel_from = {}
    if isinstance(channel_data, dict):
        maybe_from = channel_data.get("from")
        if isinstance(maybe_from, dict):
            channel_from = maybe_from

    raw_ids = [
        # Prefer sender.id as primary because it is typically present in all turns.
        getattr(sender, "id", None),
        getattr(sender, "aad_object_id", None),
        channel_from.get("id"),
        channel_from.get("aadObjectId"),
    ]

    seen: set[str] = set()
    keys: list[str] = []
    for rid in raw_ids:
        value = (rid or "").strip()
        if not value:
            continue
        key = f"{conv.id}:{value}"
        if key in seen:
            continue
        seen.add(key)
        keys.append(key)

    # Last-resort fallback key if no user identifiers were available.
    if not keys:
        keys.append(f"{conv.id}:unknown")
    return keys


# In-memory cache for Teams SSO user assertion tokens.
# Keyed by alias keys from `_session_keys` for resilient lookup.
_USER_ASSERTION_CACHE: dict[str, dict] = {}


def _extract_tenant_id(turn_context: TurnContext) -> Optional[str]:
    """Extract tenant id from Teams activity/channelData."""
    channel_data = turn_context.activity.channel_data or {}
    if isinstance(channel_data, dict):
        tenant = channel_data.get("tenant")
        if isinstance(tenant, dict):
            tenant_id = (tenant.get("id") or "").strip()
            if tenant_id:
                return tenant_id
        tenant_id = (channel_data.get("tenantId") or "").strip()
        if tenant_id:
            return tenant_id

    conversation = turn_context.activity.conversation
    maybe_tenant_id = (getattr(conversation, "tenant_id", None) or "").strip()
    return maybe_tenant_id or None


def _extract_user_token_candidates(turn_context: TurnContext) -> list[str]:
    """Extract potential user assertion tokens from known Teams payload locations."""
    candidates: list[str] = []
    seen: set[str] = set()

    def _collect_token(token: str):
        normalized = (token or "").strip()
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        candidates.append(normalized)

    def _collect_from_mapping(payload: dict):
        for key in ("token", "accessToken", "idToken", "ssoToken"):
            raw = payload.get(key)
            if isinstance(raw, str):
                _collect_token(raw)
        auth_payload = payload.get("authentication")
        if isinstance(auth_payload, dict):
            for key in ("token", "accessToken", "idToken", "ssoToken"):
                raw = auth_payload.get(key)
                if isinstance(raw, str):
                    _collect_token(raw)

    activity_value = turn_context.activity.value
    if isinstance(activity_value, dict):
        _collect_from_mapping(activity_value)

    channel_data = turn_context.activity.channel_data
    if isinstance(channel_data, dict):
        _collect_from_mapping(channel_data)

    return candidates


def _cache_user_assertion(keys: list[str], token: str) -> None:
    if not keys or not token:
        return
    try:
        from sharepoint_graph import get_unverified_jwt_claims

        claims = get_unverified_jwt_claims(token)
    except Exception:
        claims = {}

    exp = int(claims.get("exp") or 0)
    record = {"token": token, "exp": exp}
    for key in keys:
        _USER_ASSERTION_CACHE[key] = record


def _get_cached_user_assertion(keys: list[str]) -> Optional[str]:
    now_epoch = int(time.time())
    for key in keys:
        record = _USER_ASSERTION_CACHE.get(key)
        if not record:
            continue
        token = str(record.get("token") or "").strip()
        exp = int(record.get("exp") or 0)
        # keep 60s safety window for expiry
        if token and (exp == 0 or exp > now_epoch + 60):
            return token
    return None


# ------------------------------------------------------------------
# Persistent typing indicator
# ------------------------------------------------------------------
async def _keep_typing(turn_context: TurnContext, stop_event: asyncio.Event):
    """Re-send typing indicators until *stop_event* is set.

    Teams typing indicators expire after ~3 seconds.  Re-sending every
    2.5 s keeps the "Bot is typing..." badge visible for the entire
    duration of agent processing (which can be 10-60 s).
    """
    while not stop_event.is_set():
        try:
            await turn_context.send_activity(Activity(type=ActivityTypes.typing))
        except Exception:
            break
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=2.5)
            break           # event was set — stop
        except asyncio.TimeoutError:
            continue        # not set yet — send another indicator


async def _keep_typing_proactive(conversation_ref: ConversationReference, stop_event: asyncio.Event):
    """Re-send typing indicators via proactive messaging until *stop_event* is set."""
    while not stop_event.is_set():
        try:
            async def _send_typing(turn_ctx: TurnContext):
                await turn_ctx.send_activity(Activity(type=ActivityTypes.typing))
            await _send_proactive(conversation_ref, _send_typing)
        except Exception:
            break
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=2.5)
            break
        except asyncio.TimeoutError:
            continue


# ------------------------------------------------------------------
# Attachment constants
# ------------------------------------------------------------------
_IMAGE_CONTENT_TYPES = {
    "image/png", "image/jpeg", "image/jpg", "image/gif",
    "image/webp", "image/bmp", "image/tiff",
}

_IMAGE_FILE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif",
}


def _is_image_content_type(content_type: str) -> bool:
    """Check if the content type represents an image."""
    ct = (content_type or "").lower().split(";")[0].strip()
    return ct.startswith("image/")


def _is_image_filename(filename: str) -> bool:
    """Check if a filename has an image extension."""
    if not filename:
        return False
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in _IMAGE_FILE_EXTENSIONS


def _infer_image_content_type(filename: str, hint: str = "") -> Optional[str]:
    """Infer image content type from filename extension or type hints."""
    normalized_hint = (hint or "").strip().lower()
    if normalized_hint.startswith("image/") and normalized_hint != "image/*":
        return normalized_hint.split(";", 1)[0]

    ext = ""
    if filename and "." in filename:
        ext = filename.rsplit(".", 1)[-1].lower()

    if not ext and normalized_hint:
        ext = normalized_hint.replace(".", "")

    ct_map = {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "webp": "image/webp",
        "bmp": "image/bmp",
        "tiff": "image/tiff",
        "tif": "image/tiff",
    }
    return ct_map.get(ext)


def _is_image_url(url: str) -> bool:
    """Check if URL path likely points to an image file."""
    if not url:
        return False
    from urllib.parse import urlparse

    path = (urlparse(url).path or "").lower()
    for ext in _IMAGE_FILE_EXTENSIONS:
        if path.endswith(ext):
            return True
    return False


def _extract_sharepoint_links(text: str) -> list[str]:
    """Extract likely SharePoint/OneDrive URLs from message text."""
    if not text:
        return []

    import re as _re

    patterns = [
        r'https?://[^\s<>\"]*sharepoint\.com/[^\s<>\"]+',
        r'https?://1drv\.ms/[^\s<>\"]+',
    ]
    links: list[str] = []
    for pattern in patterns:
        for match in _re.findall(pattern, text, flags=_re.IGNORECASE):
            candidate = match.strip("'\")]")
            if candidate and candidate not in links:
                links.append(candidate)
    return links


def _extract_channel_data_attachments(turn_context: TurnContext) -> list[dict]:
    """Extract Teams attachments from channelData payload variants.

    Teams clients can place attachment metadata under:
    - channelData.messagePayload.attachments
    - channelData.attachments
    """
    channel_data = turn_context.activity.channel_data
    if not isinstance(channel_data, dict):
        return []

    candidates: list[dict] = []
    message_payload = channel_data.get("messagePayload")
    if isinstance(message_payload, dict):
        payload_attachments = message_payload.get("attachments")
        if isinstance(payload_attachments, list):
            candidates.extend([a for a in payload_attachments if isinstance(a, dict)])

    raw_attachments = channel_data.get("attachments")
    if isinstance(raw_attachments, list):
        candidates.extend([a for a in raw_attachments if isinstance(a, dict)])

    return candidates


def _normalize_attachment_items(turn_context: TurnContext) -> list[dict]:
    """Normalize Bot Framework and channelData attachments into one shape."""
    normalized_items: list[dict] = []

    attachments = turn_context.activity.attachments or []
    for attachment in attachments:
        normalized_items.append(
            {
                "source": "activity.attachments",
                "content_type": (getattr(attachment, "content_type", None) or "").lower().strip(),
                "filename": getattr(attachment, "name", None) or "",
                "content_url": getattr(attachment, "content_url", None) or "",
                "content": getattr(attachment, "content", None),
            }
        )

    for item in _extract_channel_data_attachments(turn_context):
        normalized_items.append(
            {
                "source": "channelData.attachments",
                "content_type": (item.get("contentType") or item.get("contenttype") or "").lower().strip(),
                "filename": item.get("name") or item.get("fileName") or item.get("filename") or "",
                "content_url": item.get("contentUrl") or item.get("contenturl") or "",
                "content": item,
            }
        )

    return normalized_items


# ------------------------------------------------------------------
# Attachment download
# ------------------------------------------------------------------
async def _get_bf_auth_header(turn_context: TurnContext) -> Optional[str]:
    """Extract a Bot Framework auth token for downloading Teams-hosted content.

    The ConnectorClient stored in turn state carries credentials that can
    produce a Bearer token accepted by Teams media endpoints
    (e.g. us-api.asm.skype.com).

    Falls back to azure.identity ManagedIdentityCredential if the
    ConnectorClient credential path fails (common with UAMI bots).
    """
    # --- Strategy A: Extract from ConnectorClient credentials ---
    try:
        connector_client = turn_context.turn_state.get(
            BotAdapter.BOT_CONNECTOR_CLIENT_KEY
        )
        if not connector_client:
            logging.warning("[Teams Bot] No ConnectorClient in turn state")
        else:
            creds = getattr(connector_client.config, "credentials", None)
            if creds is None:
                logging.warning("[Teams Bot] ConnectorClient has no credentials")
            else:
                logging.info(f"[Teams Bot] ConnectorClient creds type: {type(creds).__name__}")
                if hasattr(creds, "get_token"):
                    import inspect
                    try:
                        result = creds.get_token()
                        if inspect.isawaitable(result):
                            result = await result
                        if result:
                            token_str = getattr(result, "token", None) or str(result)
                            if token_str and token_str != "None":
                                logging.info("[Teams Bot] BF auth token obtained via ConnectorClient.get_token()")
                                return f"Bearer {token_str}"
                    except Exception as tok_err:
                        logging.warning(f"[Teams Bot] creds.get_token() failed: {tok_err}")
                if hasattr(creds, "signed_session"):
                    try:
                        sess = creds.signed_session()
                        auth = sess.headers.get("Authorization")
                        if auth:
                            logging.info("[Teams Bot] BF auth token obtained via signed_session()")
                            return auth
                    except Exception as sess_err:
                        logging.warning(f"[Teams Bot] signed_session() failed: {sess_err}")
    except Exception as e:
        logging.warning(f"[Teams Bot] ConnectorClient auth extraction failed: {e}")

    # --- Strategy B: azure.identity ManagedIdentityCredential with BF scope ---
    try:
        from azure.identity.aio import ManagedIdentityCredential
        client_id = _BOT_APP_ID or os.environ.get("AZURE_MANAGED_IDENTITY_CLIENT_ID")
        if client_id:
            logging.info(f"[Teams Bot] Trying ManagedIdentityCredential (client_id={client_id[:8]}...)")
            cred = ManagedIdentityCredential(client_id=client_id)
            try:
                token = await cred.get_token("https://api.botframework.com/.default")
                logging.info("[Teams Bot] BF auth token obtained via ManagedIdentityCredential")
                return f"Bearer {token.token}"
            finally:
                await cred.close()
        else:
            logging.warning("[Teams Bot] No BOT_APP_ID for ManagedIdentityCredential fallback")
    except Exception as e:
        logging.warning(f"[Teams Bot] ManagedIdentityCredential BF token failed: {e}")

    logging.error("[Teams Bot] ALL auth strategies failed — cannot obtain BF token for attachment download")
    return None


async def _download_attachment(
    turn_context: TurnContext,
    content_url: str,
    *,
    bf_auth_header: Optional[str] = None,
) -> Optional[bytes]:
    """Download an attachment from Teams using multiple strategies.

    Strategy 1: Use ConnectorClient from turn state (handles BF-hosted URLs)
    Strategy 2: Direct HTTP GET (works for pre-authenticated / SAS-token URLs)
    Strategy 3: Authenticated HTTP GET with Bot Framework token
    """
    if not content_url:
        return None

    # Strategy 1: Try ConnectorClient attachments API for BF connector URLs
    if "/v3/attachments/" in content_url:
        try:
            import inspect
            connector_client = turn_context.turn_state.get(
                BotAdapter.BOT_CONNECTOR_CLIENT_KEY
            )
            if connector_client and hasattr(connector_client, "attachments"):
                # Extract attachment ID from URL:
                # .../v3/attachments/{attachmentId}/views/{viewId}
                parts = content_url.split("/v3/attachments/")
                if len(parts) > 1:
                    remaining = parts[1].split("/")
                    attachment_id = remaining[0]
                    view_id = "original"
                    if "views" in remaining:
                        view_index = remaining.index("views")
                        if view_index + 1 < len(remaining):
                            view_id = remaining[view_index + 1]
                    response = await connector_client.attachments.get_attachment(
                        attachment_id, view_id
                    )
                    if hasattr(response, "read"):
                        data = response.read()
                        if inspect.isawaitable(data):
                            data = await data
                        if isinstance(data, memoryview):
                            data = data.tobytes()
                        if isinstance(data, bytearray):
                            data = bytes(data)
                        if data:
                            logging.info(
                                f"[Teams Bot] Downloaded attachment via ConnectorClient: {len(data)} bytes"
                            )
                            return data
        except Exception as e:
            logging.warning(f"[Teams Bot] ConnectorClient download failed: {e}")

    # Strategy 2: Direct HTTP GET (most Teams attachment URLs include auth in the URL)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                content_url,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    logging.info(
                        f"[Teams Bot] Downloaded attachment via HTTP GET: {len(data)} bytes"
                    )
                    return data
                logging.warning(
                    f"[Teams Bot] Direct download returned HTTP {resp.status} for {content_url[:120]}"
                )
    except Exception as e:
        logging.warning(f"[Teams Bot] Direct HTTP download failed: {e}")

    # Strategy 3: Authenticated download with Bot Framework token
    if not bf_auth_header:
        bf_auth_header = await _get_bf_auth_header(turn_context)
    if bf_auth_header:
        try:
            headers = {"Authorization": bf_auth_header}
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    content_url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        logging.info(
                            f"[Teams Bot] Downloaded attachment via authenticated GET: {len(data)} bytes"
                        )
                        return data
                    logging.warning(
                        f"[Teams Bot] Authenticated download returned HTTP {resp.status} for {content_url[:120]}"
                    )
        except Exception as e:
            logging.warning(f"[Teams Bot] Authenticated HTTP download failed: {e}")

    logging.error(f"[Teams Bot] All download strategies failed for: {content_url[:120]}")
    return None


async def _handle_audio_attachments(turn_context: TurnContext) -> Optional[str]:
    """Check for audio attachments in the message and transcribe them via Azure STT.

    Returns the transcribed text if an audio attachment was found and successfully
    transcribed, otherwise None.
    """
    attachments = turn_context.activity.attachments or []

    for attachment in attachments:
        content_type = (getattr(attachment, "content_type", None) or "").lower().strip()

        # Check if this is an audio attachment
        from speech_service import is_audio_content_type

        if not is_audio_content_type(content_type):
            continue

        content_url = getattr(attachment, "content_url", None)
        if not content_url:
            logging.warning("[Teams Bot] Audio attachment has no content_url")
            continue

        logging.info(
            f"[Teams Bot] Found audio attachment: type={content_type}, "
            f"name={getattr(attachment, 'name', 'unknown')}"
        )

        # Download the audio file
        audio_data = await _download_attachment(turn_context, content_url)
        if not audio_data:
            await turn_context.send_activity(
                "I received your voice message but couldn't download the audio file. "
                "Please try sending it again or type your message instead."
            )
            return None

        logging.info(f"[Teams Bot] Audio downloaded: {len(audio_data)} bytes, type={content_type}")

        # Transcribe using Azure Speech Service
        from speech_service import transcribe_audio

        transcribed_text = await transcribe_audio(
            audio_data=audio_data,
            content_type=content_type,
            filename=getattr(attachment, "name", None) or "voice-message.ogg",
        )

        if transcribed_text:
            logging.info(
                f"[Teams Bot] Audio transcribed: '{transcribed_text[:100]}...'"
                if len(transcribed_text) > 100
                else f"[Teams Bot] Audio transcribed: '{transcribed_text}'"
            )
            return transcribed_text
        else:
            await turn_context.send_activity(
                "I received your voice message but couldn't transcribe the audio. "
                "The recording may be too short, empty, or in an unsupported format. "
                "Please try again or type your message."
            )
            return None

    return None


# ------------------------------------------------------------------
# Image attachment handling
# ------------------------------------------------------------------
def _log_raw_attachments(turn_context: TurnContext) -> None:
    """Dump all attachment metadata for debugging."""
    attachments = turn_context.activity.attachments or []
    if not attachments:
        logging.info("[Teams Bot] No attachments on this activity")
        return
    for i, att in enumerate(attachments):
        ct = getattr(att, "content_type", None)
        cu = getattr(att, "content_url", None)
        name = getattr(att, "name", None)
        # content may be a dict (e.g. for file download info) or an adaptive card
        content = getattr(att, "content", None)
        content_summary = None
        if isinstance(content, dict):
            content_summary = {k: (str(v)[:120] if v else None) for k, v in content.items()}
        elif content is not None:
            content_summary = str(content)[:200]
        logging.info(
            f"[Teams Bot] Attachment[{i}]: contentType={ct}, name={name}, "
            f"contentUrl={str(cu)[:150] if cu else None}, content={content_summary}"
        )

    channel_attachments = _extract_channel_data_attachments(turn_context)
    if channel_attachments:
        for i, item in enumerate(channel_attachments):
            logging.info(
                "[Teams Bot] ChannelDataAttachment[%s]: contentType=%s, name=%s, contentUrl=%s, keys=%s",
                i,
                item.get("contentType") or item.get("contenttype"),
                item.get("name"),
                str(item.get("contentUrl") or item.get("contenturl") or "")[:150],
                sorted(item.keys()),
            )


async def _handle_image_attachments(turn_context: TurnContext) -> list[dict]:
    """Detect image attachments in the message, download them, and return as
    a list of dicts with keys: data (bytes), content_type (str), filename (str).

    Handles:
      1. Standard image/* attachments (inline paste, drag-drop in some clients)
      2. application/vnd.microsoft.teams.file.download.info (file uploads via +)
      3. Images embedded in HTML content with Teams CDN URLs

    Returns an empty list if no image attachments are found.
    """
    _log_raw_attachments(turn_context)

    images: list[dict] = []
    seen_urls: set[str] = set()

    normalized_items = _normalize_attachment_items(turn_context)

    # Also parse possible inline <img> tags from text body.
    text_content = turn_context.activity.text or ""
    if isinstance(text_content, str) and "<img" in text_content.lower():
        import re as _re

        for m in _re.finditer(r'<img[^>]+src=["\']([^"\']+)["\']', text_content):
            img_url = m.group(1)
            if img_url and img_url.startswith("http"):
                normalized_items.append(
                    {
                        "source": "activity.text.html",
                        "content_type": "image/png",
                        "filename": "inline-image.png",
                        "content_url": img_url,
                        "content": None,
                    }
                )

    # Pre-fetch BF auth header once for all attachments
    bf_auth_header = await _get_bf_auth_header(turn_context)

    for item in normalized_items:
        content_type = item["content_type"]
        filename = item["filename"]
        content_url = item["content_url"]
        content = item["content"]
        source = item["source"]

        if content_url:
            if content_url in seen_urls:
                continue
            seen_urls.add(content_url)

        # ------- Case 1: Standard image/* content type -------
        if _is_image_content_type(content_type):
            if not content_url:
                logging.warning("[Teams Bot] Image attachment has no content_url (source=%s)", source)
                continue
            if not filename:
                filename = "image.png"
            inferred_ct = _infer_image_content_type(filename, content_type) or "image/png"
            logging.info(
                f"[Teams Bot] Found image/* attachment: type={content_type}, name={filename}, source={source}"
            )
            image_data = await _download_attachment(
                turn_context, content_url, bf_auth_header=bf_auth_header
            )
            if not image_data:
                logging.warning(f"[Teams Bot] Failed to download image: {filename}")
                continue
            logging.info(f"[Teams Bot] Image downloaded: {len(image_data)} bytes")
            images.append({
                "data": image_data,
                "content_type": inferred_ct,
                "filename": filename,
            })
            continue

        # ------- Case 2: Teams file download info (file upload via + button) -------
        if content_type == "application/vnd.microsoft.teams.file.download.info":
            content_dict = content if isinstance(content, dict) else {}
            file_type_hint = (
                content_dict.get("fileType")
                or content_dict.get("filetype")
                or content_dict.get("type")
                or ""
            )

            # Detect image by filename, fileType hint, or URL extension.
            is_image = (
                _is_image_filename(filename)
                or bool(_infer_image_content_type(filename, file_type_hint))
                or _is_image_url(content_url)
            )
            if not is_image:
                logging.info(
                    "[Teams Bot] Skip non-image file.download.info: name=%s fileType=%s source=%s",
                    filename,
                    file_type_hint,
                    source,
                )
                continue

            download_url = ""
            if isinstance(content, dict):
                download_url = (
                    content.get("downloadUrl", "")
                    or content.get("downloadurl", "")
                    or content.get("contentUrl", "")
                    or content.get("contenturl", "")
                )
            if not download_url and content_url:
                download_url = content_url
            if not download_url:
                logging.warning(
                    f"[Teams Bot] File download info attachment has no downloadUrl: {filename}"
                )
                continue
            logging.info(
                f"[Teams Bot] Found file-download-info image: name={filename}"
            )
            image_data = await _download_attachment(
                turn_context, download_url, bf_auth_header=bf_auth_header
            )
            if not image_data:
                logging.warning(f"[Teams Bot] Failed to download file-upload image: {filename}")
                continue
            inferred_ct = _infer_image_content_type(filename, file_type_hint) or "image/png"
            logging.info(f"[Teams Bot] File-upload image downloaded: {len(image_data)} bytes")
            images.append({
                "data": image_data,
                "content_type": inferred_ct,
                "filename": filename or "image.png",
            })
            continue

        # ------- Case 3: text/html — possible inline image reference -------
        if content_type == "text/html" and isinstance(content, str):
            if images:
                # Primary image attachment already resolved; skip HTML fallback path.
                continue
            # Teams sometimes wraps inline images as an HTML <img> inside a
            # text/html attachment.  Extract the src URL.
            import re as _re
            for m in _re.finditer(r'<img[^>]+src=["\']([^"\']+)["\']', content):
                img_url = m.group(1)
                if not img_url.startswith("http"):
                    continue
                logging.info(f"[Teams Bot] Found inline image in HTML attachment: {img_url[:120]}")
                image_data = await _download_attachment(
                    turn_context, img_url, bf_auth_header=bf_auth_header
                )
                if image_data:
                    images.append({
                        "data": image_data,
                        "content_type": "image/png",
                        "filename": "inline-image.png",
                    })

    logging.info("[Teams Bot] Image extraction result: %s image(s)", len(images))

    return images


async def _handle_document_attachments(turn_context: TurnContext) -> list[dict]:
    """Detect non-image file attachments and download them.

    Primary target is Teams `application/vnd.microsoft.teams.file.download.info`
    payloads used for Office/PDF file shares.
    """
    documents: list[dict] = []
    seen_urls: set[str] = set()

    normalized_items = _normalize_attachment_items(turn_context)
    bf_auth_header = await _get_bf_auth_header(turn_context)

    for item in normalized_items:
        content_type = item["content_type"]
        filename = item["filename"] or "shared-file"
        content_url = item["content_url"]
        content = item["content"]
        source = item["source"]

        # Teams file shares usually come via this content type.
        if content_type == "application/vnd.microsoft.teams.file.download.info":
            if _is_image_filename(filename):
                continue

            download_url = ""
            if isinstance(content, dict):
                download_url = (
                    content.get("downloadUrl", "")
                    or content.get("downloadurl", "")
                    or content.get("contentUrl", "")
                    or content.get("contenturl", "")
                )
            if not download_url and content_url:
                download_url = content_url
            if not download_url:
                logging.warning(
                    "[Teams Bot] File attachment has no downloadable URL: name=%s source=%s",
                    filename,
                    source,
                )
                continue

            if download_url in seen_urls:
                continue
            seen_urls.add(download_url)

            logging.info(
                "[Teams Bot] Found file attachment: name=%s source=%s",
                filename,
                source,
            )
            file_data = await _download_attachment(
                turn_context, download_url, bf_auth_header=bf_auth_header
            )
            if not file_data:
                logging.warning("[Teams Bot] Failed to download file attachment: %s", filename)
                continue

            documents.append(
                {
                    "data": file_data,
                    "content_type": "application/octet-stream",
                    "filename": filename,
                }
            )
            continue

        # Fallback for direct non-image application/* URLs.
        if content_type.startswith("application/") and not _is_image_content_type(content_type):
            if not content_url:
                continue
            if content_url in seen_urls:
                continue
            seen_urls.add(content_url)

            logging.info(
                "[Teams Bot] Found application/* attachment: name=%s type=%s source=%s",
                filename,
                content_type,
                source,
            )
            file_data = await _download_attachment(
                turn_context, content_url, bf_auth_header=bf_auth_header
            )
            if not file_data:
                logging.warning("[Teams Bot] Failed to download application attachment: %s", filename)
                continue
            documents.append(
                {
                    "data": file_data,
                    "content_type": content_type,
                    "filename": filename,
                }
            )

    logging.info("[Teams Bot] Document extraction result: %s file(s)", len(documents))
    return documents


async def _handle_sharepoint_links_via_graph(
    *,
    sharepoint_links: list[str],
    user_assertion: str,
    tenant_id: Optional[str],
) -> list[dict]:
    """Download SharePoint/OneDrive links using Graph OBO flow."""
    if not sharepoint_links or not user_assertion:
        return []

    try:
        from sharepoint_graph import download_sharepoint_link_via_obo
    except Exception as exc:
        logging.warning("[Teams Bot] sharepoint_graph module unavailable: %s", exc)
        return []

    documents: list[dict] = []
    for link in sharepoint_links:
        logging.info("[Teams Bot] Trying Graph OBO fetch for link: %s", link[:150])
        downloaded = await download_sharepoint_link_via_obo(
            link,
            user_assertion=user_assertion,
            tenant_id=tenant_id,
        )
        if not downloaded:
            continue
        documents.append(downloaded)

    logging.info("[Teams Bot] Graph OBO link extraction result: %s file(s)", len(documents))
    return documents


# ------------------------------------------------------------------
# Proactive messaging helper
# ------------------------------------------------------------------
async def _send_proactive(conversation_ref: ConversationReference, callback):
    """Send a proactive message using the saved conversation reference."""
    await ADAPTER.continue_conversation(
        conversation_ref,
        callback,
        _BOT_APP_ID,
    )


# ------------------------------------------------------------------
# Bot logic — lightweight front door (must complete in < 10 s)
# ------------------------------------------------------------------
async def _on_message_activity(turn_context: TurnContext):
    """Handle an incoming Teams message (text or voice).

    This function runs inside the Bot Framework HTTP request.  It MUST return
    quickly (< 10 s) to avoid the 15-second gateway timeout.  Heavy work is
    dispatched to ``_process_message_background`` via ``asyncio.create_task``.
    """
    user_text = (turn_context.activity.text or "").strip()

    # Check for audio attachments (voice messages) — transcribe to text
    audio_text = await _handle_audio_attachments(turn_context)
    if audio_text:
        if user_text:
            user_text = f"{user_text}\n\n[Voice message]: {audio_text}"
        else:
            user_text = audio_text

    # Check for image attachments
    image_attachments = await _handle_image_attachments(turn_context)
    if image_attachments:
        count = len(image_attachments)
        names = ", ".join(img["filename"] for img in image_attachments)
        logging.info(f"[Teams Bot] {count} image(s) received: {names}")
        if not user_text:
            user_text = "请查看我发送的图片并描述内容。"

    # Check for non-image file attachments
    document_attachments = await _handle_document_attachments(turn_context)
    if document_attachments:
        doc_count = len(document_attachments)
        doc_names = ", ".join(doc["filename"] for doc in document_attachments)
        logging.info(f"[Teams Bot] {doc_count} document file(s) received: {doc_names}")
        if not user_text:
            user_text = "请读取我发送的文件并总结关键内容。"

    sharepoint_links = _extract_sharepoint_links(turn_context.activity.text or "")
    if sharepoint_links and not document_attachments:
        logging.info(
            "[Teams Bot] Detected SharePoint/OneDrive link(s) without downloadable attachment: %s",
            sharepoint_links,
        )

    if not user_text:
        has_any_attachments = bool(turn_context.activity.attachments or []) or bool(
            _extract_channel_data_attachments(turn_context)
        )
        if not audio_text and not has_any_attachments:
            await turn_context.send_activity(
                "Please send a text or voice message. I can understand both!"
            )
        elif has_any_attachments and not image_attachments:
            await turn_context.send_activity(
                "I received your attachment, but I could not detect a downloadable image. "
                "Please upload the image file directly or add a short text prompt with the image."
            )
        return

    # Remove bot @mention prefix that Teams adds
    user_text = _strip_bot_mention(turn_context.activity, user_text)
    if not user_text:
        await turn_context.send_activity("Please send a text message after the @mention.")
        return

    # Save conversation reference for proactive messaging later
    conversation_ref = TurnContext.get_conversation_reference(turn_context.activity)

    # Build per-user, per-conversation alias keys (group-chat safe)
    keys = _session_keys(turn_context)

    # Try to capture and cache Teams SSO user assertion tokens for Graph OBO.
    user_assertion: Optional[str] = None
    token_candidates = _extract_user_token_candidates(turn_context)
    if token_candidates:
        try:
            from sharepoint_graph import looks_like_user_assertion

            for candidate in token_candidates:
                if looks_like_user_assertion(candidate):
                    user_assertion = candidate
                    break
        except Exception as exc:
            logging.warning("[Teams Bot] Failed to validate token candidates for OBO: %s", exc)
    if user_assertion:
        _cache_user_assertion(keys, user_assertion)
    else:
        user_assertion = _get_cached_user_assertion(keys)

    # If user sent SharePoint links but no downloadable Teams file attachment,
    # attempt Graph OBO fetch using the cached/current user assertion.
    if sharepoint_links and not document_attachments:
        if user_assertion:
            tenant_id = _extract_tenant_id(turn_context)
            obo_documents = await _handle_sharepoint_links_via_graph(
                sharepoint_links=sharepoint_links,
                user_assertion=user_assertion,
                tenant_id=tenant_id,
            )
            if obo_documents:
                document_attachments.extend(obo_documents)
                logging.info(
                    "[Teams Bot] Downloaded %s SharePoint file(s) via Graph OBO",
                    len(obo_documents),
                )
                if not user_text:
                    user_text = "请读取我分享的文档并总结关键内容。"
        else:
            logging.info(
                "[Teams Bot] SharePoint links detected but no user assertion token available for OBO"
            )

    # Sender display name for context
    sender_name = ""
    if turn_context.activity.from_property:
        sender_name = getattr(turn_context.activity.from_property, "name", "") or ""

    # Send a typing indicator so the user knows we received the message
    await turn_context.send_activity(Activity(type=ActivityTypes.typing))

    # Dispatch heavy work to background — return HTTP 200 immediately
    asyncio.create_task(
        _process_message_background(
            conversation_ref=conversation_ref,
            user_text=user_text,
            keys=keys,
            sender_name=sender_name,
            images=image_attachments if image_attachments else None,
            files=document_attachments if document_attachments else None,
            sharepoint_links=(
                sharepoint_links if (sharepoint_links and not document_attachments) else None
            ),
        )
    )


# ------------------------------------------------------------------
# Background processing (runs after HTTP 200 has been returned)
# ------------------------------------------------------------------
async def _process_message_background(
    *,
    conversation_ref: ConversationReference,
    user_text: str,
    keys: list[str],
    sender_name: str,
    images: Optional[list[dict]] = None,
    files: Optional[list[dict]] = None,
    sharepoint_links: Optional[list[str]] = None,
):
    """Run agent processing and deliver the result via proactive messaging.

    This coroutine is fire-and-forget from ``_on_message_activity``.
    It owns the full lifecycle: typing indicators, agent call, file upload,
    response delivery, and error handling.
    """
    # Start persistent typing indicator via proactive channel
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(
        _keep_typing_proactive(conversation_ref, stop_typing)
    )

    try:
        # Look up existing Copilot session with alias-key fallback
        session_id = None
        for k in keys:
            existing = get_session_id(k)
            if existing:
                session_id = existing
                break

        prompt = f"[{sender_name}]: {user_text}" if sender_name else user_text

        # Prepend rolling conversation history
        history_prefix = ""
        for k in keys:
            history_prefix = render_history_for_prompt(k)
            if history_prefix:
                break
        if history_prefix:
            prompt = (
                "[System: Conversation history for context. Use it to answer follow-ups. "
                "Do not repeat it verbatim. ]\n"
                + history_prefix
                + "\n\nUser: "
                + prompt
            )

        if is_model_identity_question(user_text):
            response_text = build_runtime_model_response()

            if keys:
                try:
                    for k in keys:
                        append_turn(k, user_text, response_text)
                except Exception as exc:
                    logging.warning(f"[Teams Bot] Failed to persist conversation history: {exc}")

            stop_typing.set()

            async def _deliver_model_identity(turn_ctx: TurnContext):
                await turn_ctx.send_activity(response_text)

            await _send_proactive(conversation_ref, _deliver_model_identity)
            return

        # Create a per-request output directory for multi-user isolation
        request_output_dir = None
        try:
            from file_upload import snapshot_tmp_files, create_request_output_dir
            request_output_dir = create_request_output_dir()
            tmp_before = snapshot_tmp_files(request_output_dir)
        except Exception:
            tmp_before = {}

        agent_prompt = prompt

        # Teams chat window is narrow — instruct the agent to be concise
        teams_style = (
            "[System: You are replying in a Teams chat window. "
            "Keep responses concise and well-structured: use short paragraphs, "
            "bullet points, and avoid lengthy preambles or repetition. "
            "Aim for the minimum effective response length.]\n\n"
        )
        image_style = ""
        if images:
            image_names = ", ".join((img.get("filename") or "image") for img in images)
            image_style = (
                "[System: The user attached image file(s) to this message. "
                "You must inspect the attached image(s) before answering any image-related question. "
                f"Attached filenames: {image_names}.]\n\n"
            )
        file_style = ""
        if files:
            file_names = ", ".join((f.get("filename") or "file") for f in files)
            file_style = (
                "[System: The user attached document file(s) to this message. "
                "You must read/analyze these attached files before answering file-related questions. "
                f"Attached filenames: {file_names}.]\n\n"
            )
        sharepoint_style = ""
        if sharepoint_links and not files:
            links_text = "\n".join(sharepoint_links)
            sharepoint_style = (
                "[System: The user shared SharePoint/OneDrive URL(s) but no downloadable file attachment "
                "was provided in this Teams payload. Private enterprise URLs are usually inaccessible "
                "without delegated Microsoft Graph permissions. If the user needs file analysis, ask them "
                "to upload the file directly in Teams (so it is sent as file.download.info) or provide "
                "a publicly accessible URL.]\n"
                f"Detected link(s):\n{links_text}\n\n"
            )
        if request_output_dir:
            agent_prompt = (
                teams_style
                + image_style
                + file_style
                + sharepoint_style
                + f"[System: Write all generated files to {request_output_dir}/ — "
                f"this is your dedicated output directory for this request.]\n\n{prompt}"
            )
        else:
            agent_prompt = teams_style + image_style + file_style + sharepoint_style + prompt

        result = await run_copilot_agent(
            agent_prompt,
            session_id=session_id,
            images=images,
            files=files,
        )

        # ── diagnostic print (stdout captured even outside function context) ──
        import sys as _sys
        print(
            f"[BG-DIAG] session_id={result.session_id} | "
            f"tool_calls={json.dumps(result.tool_calls, default=str)[:1000]} | "
            f"response_head={( result.content or '')[:300]}",
            file=_sys.stdout,
            flush=True,
        )

        # Store session mapping
        if result.session_id:
            for k in keys:
                set_session_id(k, result.session_id)

        response_text = result.content or "(No response from agent)"

        # Persist conversation history
        if keys:
            try:
                for k in keys:
                    append_turn(k, user_text, response_text)
            except Exception as exc:
                logging.warning(f"[Teams Bot] Failed to persist conversation history: {exc}")

        # Upload generated files
        uploaded_files = []
        downloads_card: Optional[Activity] = None
        try:
            from file_upload import (
                snapshot_tmp_files, find_new_files, upload_and_replace,
                find_file_paths_in_text,
            )
            tmp_after = snapshot_tmp_files(request_output_dir) if request_output_dir else snapshot_tmp_files()
            new_files = find_new_files(tmp_before, tmp_after)

            has_tmp_ref = "/tmp/" in response_text
            text_paths = find_file_paths_in_text(response_text) if has_tmp_ref else []
            text_paths_exist = {p: os.path.isfile(p) for p in text_paths}

            logging.info(
                f"[Teams Bot] File detection: before={len(tmp_before)}, "
                f"after={len(tmp_after)}, new_files={new_files}, "
                f"has_tmp_ref={has_tmp_ref}, text_paths={text_paths}, "
                f"text_paths_exist={text_paths_exist}, "
                f"output_dir={request_output_dir}"
            )

            if new_files or has_tmp_ref:
                response_text, uploaded_files = await asyncio.to_thread(
                    upload_and_replace, response_text, new_files, False
                )
                if uploaded_files:
                    logging.info(
                        f"[Teams Bot] Uploaded {len(uploaded_files)} file(s) to blob storage"
                    )
                    max_buttons = 6
                    buttons: list[CardAction] = []
                    for uf in uploaded_files[:max_buttons]:
                        if uf.download_url:
                            buttons.append(
                                CardAction(
                                    type=ActionTypes.open_url,
                                    title=f"Download: {uf.filename}",
                                    value=uf.download_url,
                                )
                            )
                    if buttons:
                        card = HeroCard(
                            title="Downloads",
                            text="Your generated file(s) are ready.",
                            buttons=buttons,
                        )
                        downloads_card = Activity(
                            type=ActivityTypes.message,
                            attachments=[Attachment(content_type="application/vnd.microsoft.card.hero", content=card)],
                        )
                else:
                    logging.warning(
                        f"[Teams Bot] upload_and_replace returned 0 files; "
                        f"new_files={new_files}, text_paths={text_paths}"
                    )
            else:
                logging.info("[Teams Bot] No /tmp references and no new files; skipping upload")
        except Exception as upload_exc:
            logging.error(
                f"[Teams Bot] File upload pipeline error: {upload_exc}", exc_info=True
            )

        # Truncate for Teams 28KB limit
        if len(response_text) > 25000:
            response_text = response_text[:25000] + "\n\n... (truncated)"

        # Stop typing before delivering response
        stop_typing.set()

        # Deliver response via proactive messaging
        final_response_text = response_text
        final_downloads_card = downloads_card

        async def _deliver(turn_ctx: TurnContext):
            await turn_ctx.send_activity(final_response_text)
            if final_downloads_card:
                await turn_ctx.send_activity(final_downloads_card)

        await _send_proactive(conversation_ref, _deliver)

    except Exception as exc:
        stop_typing.set()
        err_str = str(exc)
        logging.error(f"[Teams Bot] background agent error: {exc}", exc_info=True)

        if "502" in err_str or "ProxyResponseError" in err_str or "firewall" in err_str.lower():
            error_msg = (
                "The agent encountered a temporary connectivity issue with the AI backend (HTTP 502). "
                "This is usually transient. Please try again in a moment."
            )
        else:
            error_msg = f"Sorry, the agent encountered an error: {err_str[:500]}"

        async def _deliver_error(turn_ctx: TurnContext):
            await turn_ctx.send_activity(error_msg)

        try:
            await _send_proactive(conversation_ref, _deliver_error)
        except Exception:
            logging.error("[Teams Bot] Failed to deliver error via proactive messaging", exc_info=True)
    finally:
        stop_typing.set()
        try:
            typing_task.cancel()
            await typing_task
        except (asyncio.CancelledError, Exception):
            pass


def _strip_bot_mention(activity: Activity, text: str) -> str:
    """Remove the @mention of the bot from the message text."""
    if activity.entities:
        for entity in activity.entities:
            if entity.type == "mention":
                mentioned = getattr(entity, "mentioned", None)
                if mentioned and getattr(mentioned, "id", None) == activity.recipient.id:
                    mention_text = getattr(entity, "text", "")
                    if mention_text:
                        text = text.replace(mention_text, "").strip()
    return text


async def handle_incoming_activity(body: bytes, auth_header: str) -> tuple[int, str]:
    """Process an incoming Bot Framework activity.

    Returns (status_code, response_body).
    """
    activity = Activity().deserialize(json.loads(body))

    async def _bot_callback(turn_context: TurnContext):
        if turn_context.activity.type == ActivityTypes.message:
            await _on_message_activity(turn_context)
        elif turn_context.activity.type == ActivityTypes.conversation_update:
            # In production, do not proactively send welcome messages unless explicitly enabled.
            if _should_send_welcome_message() and turn_context.activity.members_added:
                for member in turn_context.activity.members_added:
                    if member.id != turn_context.activity.recipient.id:
                        await turn_context.send_activity(
                            "Hello! I'm HaeronClaw — your Microsoft expert for internal FAQ, Azure pricing, tech questions, compete advisory, and marketing content support. You can also send me voice messages!"
                        )
        elif turn_context.activity.type == ActivityTypes.invoke:
            # Handle invoke activities (e.g., adaptive card actions)
            logging.info(f"[Teams Bot] invoke activity: {turn_context.activity.name}")
            keys = _session_keys(turn_context)
            token_candidates = _extract_user_token_candidates(turn_context)
            if keys and token_candidates:
                try:
                    from sharepoint_graph import looks_like_user_assertion

                    for candidate in token_candidates:
                        if looks_like_user_assertion(candidate):
                            _cache_user_assertion(keys, candidate)
                            logging.info("[Teams Bot] Cached user assertion token from invoke activity")
                            break
                except Exception as exc:
                    logging.warning(
                        "[Teams Bot] Failed to process invoke token candidates: %s",
                        exc,
                    )
        else:
            logging.info(f"[Teams Bot] ignoring activity type: {turn_context.activity.type}")

    response = await ADAPTER.process_activity(auth_header, activity, _bot_callback)

    if response:
        return response.status, response.body if hasattr(response, 'body') else ""
    return HTTPStatus.OK, ""
