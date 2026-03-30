import os

from .config import resolve_config_dir, session_exists

# Restore m365-cli credentials from Key Vault on cold start (best-effort, non-blocking)
try:
    from .m365_credentials import restore_m365_credentials
    restore_m365_credentials()
except Exception:
    pass  # Non-critical — m365 tools simply won't authenticate

# When AZURE_AI_FOUNDRY_ENDPOINT is set *and* no GITHUB_TOKEN is available
# (or the token can't authenticate with the Copilot SDK), use the direct
# Azure OpenAI runner which bypasses the Copilot SDK entirely.
_USE_DIRECT_RUNNER = bool(
    os.environ.get("AZURE_AI_FOUNDRY_ENDPOINT")
    and os.environ.get("USE_DIRECT_OPENAI", "").lower() in ("1", "true", "yes")
)

if _USE_DIRECT_RUNNER:
    from .direct_openai_runner import (
        AgentResult,
        DEFAULT_MODEL,
        DEFAULT_TIMEOUT,
        run_direct_agent as run_copilot_agent,
        run_direct_agent_stream as run_copilot_agent_stream,
    )
else:
    from .runner import (
        AgentResult,
        DEFAULT_MODEL,
        DEFAULT_TIMEOUT,
        run_copilot_agent,
        run_copilot_agent_stream,
    )

__all__ = [
    "AgentResult",
    "DEFAULT_MODEL",
    "DEFAULT_TIMEOUT",
    "resolve_config_dir",
    "run_copilot_agent",
    "run_copilot_agent_stream",
    "session_exists",
]
