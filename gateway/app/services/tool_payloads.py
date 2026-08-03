"""Safe, bounded projection of tool inputs and outputs for public history."""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

MAX_TOOL_PAYLOAD_BYTES = 256 * 1024
PREVIEW_BYTES = 8 * 1024
SENSITIVE_FIELD = re.compile(r"(?:api[_-]?key|token|secret|password|authorization|credential|cookie)", re.IGNORECASE)


def _json_value(value: Any, secrets: tuple[str, ...], seen: set[int] | None = None, depth: int = 0) -> Any:
    if depth > 64:
        return "[Depth limit]"
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value
    if value is None or isinstance(value, bool | int | float):
        return value
    if seen is None:
        seen = set()
    if isinstance(value, (dict, list, tuple)):
        identity = id(value)
        if identity in seen:
            return "[Circular]"
        seen.add(identity)
        if isinstance(value, dict):
            return {
                str(key): "[REDACTED]" if SENSITIVE_FIELD.search(str(key)) else _json_value(item, secrets, seen, depth + 1)
                for key, item in value.items()
            }
        return [_json_value(item, secrets, seen, depth + 1) for item in value]
    return str(value)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")


def safe_tool_payload(value: Any, secret_values: Iterable[str] = ()) -> tuple[Any, bool]:
    """Return JSON-safe payload plus its truncation marker; never log ``value``."""
    # Measure the original without returning it.  A large value hidden by a
    # sensitive-key redaction is still a large incoming tool payload.
    try:
        original_size = len(_json_bytes(value))
    except (TypeError, ValueError):
        original_size = 0
    safe = _json_value(value, tuple(item for item in secret_values if item))
    serialized = _json_bytes(safe)
    if original_size <= MAX_TOOL_PAYLOAD_BYTES:
        return safe, False
    preview = serialized[:PREVIEW_BYTES].decode("utf-8", errors="ignore")
    return {"preview": preview, "original_bytes": original_size}, True


def safe_tool_content(value: Any, fallback: str, secret_values: Iterable[str] = ()) -> str:
    safe, _ = safe_tool_payload(value, secret_values)
    if isinstance(safe, str) and safe:
        return safe
    rendered = _json_bytes(safe).decode("utf-8")
    return rendered or fallback
