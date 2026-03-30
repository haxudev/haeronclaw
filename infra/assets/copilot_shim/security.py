"""Security hardening for the agent runtime.

Provides:
- Custom permission handler that replaces PermissionHandler.approve_all
- Path validation utilities to enforce /tmp-only write policy
- Skill / source-code access blocking
- Security audit logging
"""

import logging
import os
import re
from typing import Any, Dict

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Allowed / denied path patterns
# ---------------------------------------------------------------------------

# Only /tmp (and subdirectories) is writable
_WRITABLE_PREFIXES = ("/tmp/", "/tmp")

# Paths that must NEVER be read or written by the agent (source code & config)
_PROTECTED_PATH_PATTERNS: list[re.Pattern] = [
    re.compile(r"/home/site/wwwroot/", re.IGNORECASE),
    re.compile(r"function_app\.py", re.IGNORECASE),
    re.compile(r"host\.json", re.IGNORECASE),
    re.compile(r"copilot_shim/", re.IGNORECASE),
    re.compile(r"copilot_shim\\", re.IGNORECASE),
    re.compile(r"AGENTS\.md", re.IGNORECASE),
]

# Skill files must never be disclosed to users
_SKILL_PATH_PATTERNS: list[re.Pattern] = [
    re.compile(r"SKILL\.md", re.IGNORECASE),
    re.compile(r"\.github/skills/", re.IGNORECASE),
    re.compile(r"\.github\\skills\\", re.IGNORECASE),
    re.compile(r"/skills/.*SKILL\.md", re.IGNORECASE),
]

# Shell commands that are always blocked
_BLOCKED_SHELL_PATTERNS: list[re.Pattern] = [
    re.compile(r"\bsudo\b"),
    re.compile(r"\bapt-get\b"),
    re.compile(r"\bapt\b"),
    re.compile(r"\bpip\s+install\b"),
    re.compile(r"\brm\s+-rf\s+/"),
    re.compile(r"\bchmod\b"),
    re.compile(r"\bchown\b"),
    # Block writing/overwriting source files via shell
    re.compile(r">\s*/home/site/wwwroot/"),
    re.compile(r"\btee\s+/home/site/wwwroot/"),
    re.compile(r"\bcp\b.*\s+/home/site/wwwroot/"),
    re.compile(r"\bmv\b.*\s+/home/site/wwwroot/"),
    re.compile(r"\bsed\s+-i\b.*\s+/home/site/wwwroot/"),
    # Block packaging/tar/zip of skill directories
    re.compile(r"\btar\b.*skills"),
    re.compile(r"\bzip\b.*skills"),
    re.compile(r"\bcat\b.*SKILL\.md"),
    # Block sending source code via curl/wget
    re.compile(r"\bcurl\b.*--upload-file\b"),
    re.compile(r"\bcurl\b.*-T\b"),
]


def is_path_writable(path: str) -> bool:
    """Check if a path is within the allowed writable area (/tmp only)."""
    normalized = os.path.normpath(path).replace("\\", "/")
    return normalized.startswith("/tmp/") or normalized == "/tmp"


def is_path_protected(path: str) -> bool:
    """Check if a path matches a protected source-code or config pattern."""
    for pattern in _PROTECTED_PATH_PATTERNS:
        if pattern.search(path):
            return True
    return False


def is_skill_path(path: str) -> bool:
    """Check if a path refers to a skill definition file."""
    for pattern in _SKILL_PATH_PATTERNS:
        if pattern.search(path):
            return True
    return False


def is_shell_command_blocked(command: str) -> bool:
    """Check if a shell command matches any blocked pattern."""
    for pattern in _BLOCKED_SHELL_PATTERNS:
        if pattern.search(command):
            return True
    return False


# ---------------------------------------------------------------------------
# Custom Permission Handler for Copilot SDK
# ---------------------------------------------------------------------------

def secure_permission_handler(
    request: Dict[str, Any], context: Dict[str, str]
) -> Dict[str, Any]:
    """Permission handler that enforces security policies.

    Replaces PermissionHandler.approve_all to enforce:
    - Write operations only to /tmp
    - Shell commands cannot modify source code or access skills
    - Read operations cannot access skill definitions
    - All permission decisions are logged for audit
    """
    kind = request.get("kind", "")
    tool_call_id = request.get("toolCallId", "")

    # Extract additional context that the Copilot CLI may include
    # (path for write/read, command for shell)
    path = request.get("path", "") or request.get("filePath", "") or ""
    command = request.get("command", "") or ""

    _logger.info(
        f"[Security] Permission request: kind={kind}, toolCallId={tool_call_id}, "
        f"path={path[:100]}, command={command[:100]}"
    )

    # --- WRITE operations: only /tmp is allowed ---
    if kind == "write":
        if path and not is_path_writable(path):
            _logger.warning(
                f"[Security] DENIED write to non-/tmp path: {path}"
            )
            return {
                "kind": "denied-by-rules",
                "rules": [f"Write operations are only allowed under /tmp. Attempted path: {path}"],
            }
        if path and (is_path_protected(path) or is_skill_path(path)):
            _logger.warning(
                f"[Security] DENIED write to protected path: {path}"
            )
            return {
                "kind": "denied-by-rules",
                "rules": ["Writing to source code or skill files is prohibited."],
            }
        # Allow writes to /tmp or when path is not specified (let the tool handle it)
        _logger.info(f"[Security] Approved write: {path or '(path not in request)'}")
        return {"kind": "approved", "rules": []}

    # --- SHELL operations: block dangerous commands ---
    if kind == "shell":
        if command and is_shell_command_blocked(command):
            _logger.warning(
                f"[Security] DENIED blocked shell command: {command[:200]}"
            )
            return {
                "kind": "denied-by-rules",
                "rules": ["This shell command is blocked by security policy."],
            }
        # Log all shell commands for audit
        _logger.info(f"[Security] Approved shell: {command[:200] or '(command not in request)'}")
        return {"kind": "approved", "rules": []}

    # --- READ operations: block access to skill files and protected source ---
    if kind == "read":
        if path and is_skill_path(path):
            _logger.warning(
                f"[Security] DENIED read of skill file: {path}"
            )
            return {
                "kind": "denied-by-rules",
                "rules": ["Reading skill definition files is prohibited."],
            }
        if path and is_path_protected(path):
            _logger.warning(
                f"[Security] DENIED read of protected source file: {path}"
            )
            return {
                "kind": "denied-by-rules",
                "rules": ["Reading source code files is prohibited."],
            }
        _logger.info(f"[Security] Approved read: {path or '(path not in request)'}")
        return {"kind": "approved", "rules": []}

    # --- MCP / URL: allow by default, log for audit ---
    _logger.info(f"[Security] Approved {kind}: toolCallId={tool_call_id}")
    return {"kind": "approved", "rules": []}


# ---------------------------------------------------------------------------
# Prompt injection detection (basic heuristic)
# ---------------------------------------------------------------------------

_INJECTION_PATTERNS: list[re.Pattern] = [
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions|rules|prompts)", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+(a\s+)?new\s+(AI|assistant|agent)", re.IGNORECASE),
    re.compile(r"system\s*:\s*you\s+are", re.IGNORECASE),
    re.compile(r"disregard\s+(all\s+)?(safety|security|rules)", re.IGNORECASE),
    re.compile(r"override\s+(security|safety|rules|policies|instructions)", re.IGNORECASE),
    re.compile(r"print\s+your\s+(system\s+)?prompt", re.IGNORECASE),
    re.compile(r"show\s+(me\s+)?your\s+(system\s+)?(prompt|instructions|rules)", re.IGNORECASE),
    re.compile(r"reveal\s+your\s+(system\s+)?(prompt|instructions|context)", re.IGNORECASE),
    re.compile(r"output\s+your\s+(system\s+)?(prompt|instructions)", re.IGNORECASE),
    re.compile(r"repeat\s+(the\s+)?(text|words|content)\s+(above|before)", re.IGNORECASE),
]


def detect_prompt_injection(text: str) -> bool:
    """Basic heuristic check for common prompt injection patterns.

    Returns True if the text looks like a prompt injection attempt.
    This is a defense-in-depth measure — the primary defense is the
    system prompt security rules.
    """
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(text):
            _logger.warning(f"[Security] Possible prompt injection detected: {text[:200]}")
            return True
    return False


# ---------------------------------------------------------------------------
# Content output sanitizer — strip skill/source paths from responses
# ---------------------------------------------------------------------------

_SENSITIVE_PATH_PATTERNS: list[re.Pattern] = [
    re.compile(r"(\.github/skills/[^\s]+/SKILL\.md)"),
    re.compile(r"(/home/site/wwwroot/copilot_shim/[^\s]+)"),
    re.compile(r"(/home/site/wwwroot/function_app\.py)"),
    re.compile(r"(/home/site/wwwroot/AGENTS\.md)"),
]


def sanitize_output(text: str) -> str:
    """Remove sensitive file paths from agent output text."""
    result = text
    for pattern in _SENSITIVE_PATH_PATTERNS:
        result = pattern.sub("[redacted-internal-path]", result)
    return result
