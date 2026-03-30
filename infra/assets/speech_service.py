"""
Audio transcription (STT) using Azure OpenAI gpt-audio-1.5 model.

Uses the OpenAI Chat Completions API with input_audio content parts to
transcribe voice messages. Authentication is via Managed Identity (Entra ID)
using the 'Cognitive Services OpenAI User' role — no Speech-specific RBAC
(Cognitive Services Speech User) is required.

Why gpt-audio-1.5 instead of Azure Speech Service?
  The deployer lacks Microsoft.Authorization/roleAssignments/write, so the
  'Cognitive Services Speech User' role cannot be assigned.  gpt-audio-1.5
  operates under the OpenAI data plane, which only requires the already-
  assigned 'Cognitive Services OpenAI User' role.
"""

import base64
import logging
import os
from typing import Optional

from azure.identity.aio import DefaultAzureCredential, ManagedIdentityCredential
from openai import AsyncAzureOpenAI

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# The Azure OpenAI endpoint, e.g. https://your-resource-name.openai.azure.com/
OPENAI_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
AUDIO_MODEL = os.environ.get("AZURE_AUDIO_MODEL", "gpt-audio-1.5")
MI_CLIENT_ID = os.environ.get("AZURE_MANAGED_IDENTITY_CLIENT_ID", "")

API_VERSION = "2025-04-01-preview"
_COGNITIVE_SERVICES_SCOPE = "https://cognitiveservices.azure.com/.default"

# Supported audio content types for STT
SUPPORTED_AUDIO_TYPES = {
    "audio/ogg",
    "audio/ogg; codecs=opus",
    "audio/wav",
    "audio/wave",
    "audio/x-wav",
    "audio/mp3",
    "audio/mpeg",
    "audio/mp4",
    "audio/x-m4a",
    "audio/webm",
    "audio/webm; codecs=opus",
    "audio/flac",
    "audio/x-flac",
    "audio/aac",
}

# Map MIME base types to OpenAI input_audio format strings
_FORMAT_MAP = {
    "audio/ogg": "ogg",
    "audio/wav": "wav",
    "audio/wave": "wav",
    "audio/x-wav": "wav",
    "audio/mp3": "mp3",
    "audio/mpeg": "mp3",
    "audio/mp4": "mp4",
    "audio/x-m4a": "mp4",
    "audio/webm": "webm",
    "audio/flac": "flac",
    "audio/x-flac": "flac",
    "audio/aac": "aac",
}


# ---------------------------------------------------------------------------
# Token acquisition
# ---------------------------------------------------------------------------
async def _get_entra_token() -> str:
    """Acquire an Entra ID token for Cognitive Services using Managed Identity."""
    if MI_CLIENT_ID:
        credential = ManagedIdentityCredential(client_id=MI_CLIENT_ID)
    else:
        credential = DefaultAzureCredential()

    try:
        token = await credential.get_token(_COGNITIVE_SERVICES_SCOPE)
        return token.token
    finally:
        await credential.close()


# ---------------------------------------------------------------------------
# Speech-to-Text (STT) via gpt-audio-1.5
# ---------------------------------------------------------------------------
async def transcribe_audio(
    audio_data: bytes,
    content_type: str = "audio/ogg",
    filename: str = "voice-message.ogg",
    locales: Optional[list[str]] = None,
) -> Optional[str]:
    """
    Transcribe audio using Azure OpenAI gpt-audio-1.5 via Chat Completions API.

    Sends the raw audio as a base64-encoded ``input_audio`` content part and
    instructs the model to return a faithful transcription.  Language is
    auto-detected by the model (the *locales* parameter is accepted for API
    compatibility but currently unused).

    Args:
        audio_data: Raw audio bytes
        content_type: MIME type of the audio (e.g. audio/ogg, audio/wav)
        filename: Filename hint (kept for API compat, unused by this impl)
        locales: Language hints (kept for API compat, unused — model auto-detects)

    Returns:
        Transcribed text, or None if transcription failed
    """
    if not OPENAI_ENDPOINT:
        logger.error("AZURE_OPENAI_ENDPOINT not configured — cannot transcribe audio")
        return None

    if not audio_data:
        logger.warning("Empty audio data provided for transcription")
        return None

    # Determine audio format from content type
    base_ct = content_type.split(";")[0].strip().lower()
    audio_format = _FORMAT_MAP.get(base_ct, "ogg")

    # Base64-encode the audio for the OpenAI API
    b64_audio = base64.b64encode(audio_data).decode("utf-8")
    logger.info(
        f"STT: preparing {len(audio_data)} bytes ({base_ct} -> {audio_format}) "
        f"for {AUDIO_MODEL}"
    )

    token = await _get_entra_token()

    client = AsyncAzureOpenAI(
        azure_endpoint=OPENAI_ENDPOINT,
        azure_ad_token=token,
        api_version=API_VERSION,
    )

    try:
        response = await client.chat.completions.create(
            model=AUDIO_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a transcription assistant. "
                        "Transcribe the following audio message exactly as spoken. "
                        "Output ONLY the transcribed text — no commentary, no formatting, "
                        "no quotation marks. Preserve the original language of the speaker. "
                        "If the audio is empty or unintelligible, respond with exactly: [EMPTY]"
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": b64_audio,
                                "format": audio_format,
                            },
                        }
                    ],
                },
            ],
            modalities=["text"],
            temperature=0,
        )

        text = (response.choices[0].message.content or "").strip()

        if text and text != "[EMPTY]":
            logger.info(
                f"STT transcription successful ({AUDIO_MODEL}): {len(text)} chars"
            )
            return text

        logger.warning(f"STT returned empty/unintelligible result from {AUDIO_MODEL}")
        return None

    except Exception as e:
        logger.error(f"STT error ({AUDIO_MODEL}): {e}", exc_info=True)
        return None
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------
def is_audio_content_type(content_type: str) -> bool:
    """Check if a MIME content type is a supported audio format."""
    if not content_type:
        return False
    # Normalize and check against supported types
    ct = content_type.lower().strip()
    # Check exact match first
    if ct in SUPPORTED_AUDIO_TYPES:
        return True
    # Check base type (without parameters like codecs=opus)
    base_ct = ct.split(";")[0].strip()
    return base_ct.startswith("audio/")
