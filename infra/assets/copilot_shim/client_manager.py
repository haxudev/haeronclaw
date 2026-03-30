import asyncio
import logging
import os
from typing import Optional

from copilot import CopilotClient

from .cli_path import get_copilot_cli_path


def _is_byok_mode() -> bool:
    """Check if BYO model (Microsoft Foundry) environment variables are configured.

    Returns True when AZURE_AI_FOUNDRY_ENDPOINT is set, regardless of whether
    an API key is provided.  When no API key is present the runner will use
    Entra ID (managed-identity) authentication instead.
    """
    return bool(os.environ.get("AZURE_AI_FOUNDRY_ENDPOINT"))


class CopilotClientManager:
    """
    Singleton manager for the CopilotClient.
    """

    _instance: Optional["CopilotClientManager"] = None
    _client: Optional[CopilotClient] = None
    _lock: asyncio.Lock = None
    _started: bool = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._lock = asyncio.Lock()
        return cls._instance

    @classmethod
    async def get_client(cls) -> CopilotClient:
        manager = cls()
        async with manager._lock:
            if manager._client is None or not manager._started:
                cli_path = get_copilot_cli_path()

                github_token = os.environ.get("GITHUB_TOKEN")

                if _is_byok_mode():
                    logging.info(f"BYOK mode: using Microsoft Foundry (github_token present: {github_token is not None})")
                    client_config = {"cli_path": cli_path}
                    # The Copilot SDK still needs a GitHub token for session
                    # management even in BYOK mode where the model provider
                    # is overridden via SessionConfig.provider.
                    if github_token:
                        client_config["github_token"] = github_token
                    manager._client = CopilotClient(client_config)  # type: ignore
                else:
                    logging.info(f"Standard mode (github_token present: {github_token is not None})")
                    manager._client = CopilotClient(
                        {
                            "cli_path": cli_path,
                            "github_token": github_token,
                        }  # type: ignore
                    )

                await manager._client.start()
                manager._started = True
                logging.info(f"CopilotClient singleton started (CLI: {cli_path}, BYOK: {_is_byok_mode()})")
        return manager._client

    @classmethod
    async def create_ephemeral_client(cls, github_token: str) -> CopilotClient:
        """Create a short-lived CopilotClient bound to a caller-provided token.

        This is used for per-user OAuth access tokens so the app does not rely on
        a long-lived service token stored in app settings.
        """
        if not github_token:
            raise ValueError("github_token is required for ephemeral client")

        cli_path = get_copilot_cli_path()
        client = CopilotClient(
            {
                "cli_path": cli_path,
                "github_token": github_token,
                # Enforce explicit token usage and avoid any local credential fallback.
                "use_logged_in_user": False,
            }  # type: ignore
        )
        await client.start()
        logging.info(f"Ephemeral CopilotClient started (CLI: {cli_path})")
        return client

    @classmethod
    async def shutdown(cls):
        manager = cls()
        async with manager._lock:
            if manager._client and manager._started:
                await manager._client.stop()
                manager._started = False
                manager._client = None
                logging.info("CopilotClient singleton stopped")

    @classmethod
    def is_running(cls) -> bool:
        manager = cls()
        return manager._started
