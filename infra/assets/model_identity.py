import os
import re


_MODEL_QUESTION_PATTERNS = [
    re.compile(r"(你|您|当前|现在|背后).{0,12}(是什么|用的|正在用|运行).{0,12}模型", re.IGNORECASE),
    re.compile(r"什么模型", re.IGNORECASE),
    re.compile(r"哪个模型", re.IGNORECASE),
    re.compile(r"\b(what|which)\b.{0,24}\bmodel\b", re.IGNORECASE),
    re.compile(r"\bare you\s+(claude|gpt)\b", re.IGNORECASE),
]


def is_model_identity_question(text: str | None) -> bool:
    if not text:
        return False

    normalized = text.strip()
    return any(pattern.search(normalized) for pattern in _MODEL_QUESTION_PATTERNS)


def get_runtime_model_name() -> str:
    return (
        os.environ.get("COPILOT_MODEL")
        or os.environ.get("AZURE_AI_FOUNDRY_MODEL")
        or "unknown"
    ).strip() or "unknown"


def build_runtime_model_response() -> str:
    return f"当前运行时配置的模型是 {get_runtime_model_name()}。"