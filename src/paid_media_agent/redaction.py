"""Secret redaction for tool results, errors, and logs."""

from __future__ import annotations

import re
from collections.abc import Sequence

REDACTED = "[REDACTED]"

_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)bearer\s+[A-Za-z0-9\-._~+/]+=*"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{8,}\b"),
    re.compile(r"\bxapp-[A-Za-z0-9-]{8,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"\bpb_[A-Za-z0-9_-]{16,}\b"),
    re.compile(
        r"(?i)(api[_-]?key|token|secret|password|authorization)\s*[:=]\s*['\"]?[A-Za-z0-9\-._~+/]{12,}['\"]?"
    ),
    re.compile(r"(?i)postgres(?:ql)?://[^\s'\"]+"),
)


def redact(text: str, secrets: Sequence[str] = ()) -> str:
    """Remove known secret values and credential-shaped substrings."""
    result = text
    for secret in secrets:
        if secret and len(secret) >= 6:
            result = result.replace(secret, REDACTED)
    for pattern in _PATTERNS:
        result = pattern.sub(REDACTED, result)
    return result


def sanitize_exception(exc: BaseException, secrets: Sequence[str] = ()) -> str:
    """Return a bounded, redacted, one-line description that is safe for the model."""
    text = f"{type(exc).__name__}: {exc}"
    text = text.replace("\n", " ")
    return redact(text, secrets)[:400]
