"""
Redaction -- the single choke point every log/evidence write must pass
through. Two independent redaction sources, applied together:

1. Schema-driven: any InputParam marked `pii=True` has its *value*
   replaced wherever it appears in logged text (not just in a dict field
   -- also inline in freeform LLM reasoning strings, which is where PII
   actually leaks in agent systems).
2. Pattern-driven: a conservative regex net for things nobody should ever
   type into a config (SSNs, card numbers, obvious secret-shaped tokens)
   as defense-in-depth, independent of whether a param was correctly
   tagged pii=True.

This runs on the discovery transcript, the evidence logs, and the replay
result -- never selectively. Redaction that only covers "the fields I
remembered to tag" is not a guardrail, so pattern-based redaction always
runs regardless of schema tagging.
"""

from __future__ import annotations

import re

_PATTERNS = [
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),               # SSN-shaped
    re.compile(r"\b(?:\d[ -]*?){13,19}\b"),               # card-number-shaped
    re.compile(r"(?i)\b(api[_-]?key|secret|password|token)\b\s*[:=]\s*\S+"),
]

_REDACTED = "[REDACTED]"


def redact_text(text: str, sensitive_values: list[str] | None = None) -> str:
    """Redact known sensitive literal values, then sweep with pattern-based defense-in-depth."""

    result = text
    for value in sensitive_values or []:
        if value:
            result = result.replace(value, _REDACTED)
    for pattern in _PATTERNS:
        result = pattern.sub(_REDACTED, result)
    return result


def redact_dict(data: dict, sensitive_keys: set[str]) -> dict:
    """Shallow redaction for structured payloads (e.g. an artifact's input bindings before logging)."""

    return {k: (_REDACTED if k in sensitive_keys else v) for k, v in data.items()}
