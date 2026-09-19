"""Small dotenv helpers shared by black-box acceptance scripts."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path


ENV_ASSIGNMENT = re.compile(r"^(?P<prefix>\s*)(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=")


def parse_dotenv(path: Path) -> dict[str, str]:
    """Read the dotenv subset used by the repository's acceptance scripts."""
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith("#") or "=" not in candidate:
            continue
        key, value = candidate.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] == '"':
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = value[1:-1]
        elif len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1]
        values[key] = value
    return values


def configured_value(name: str, dotenv_values: dict[str, str]) -> str | None:
    """Prefer an explicit process value, then use the repository-local .env."""
    return os.getenv(name) or dotenv_values.get(name)


def update_dotenv(path: Path, updates: dict[str, str]) -> None:
    """Atomically replace or append dotenv assignments while preserving other lines."""
    require_single_line = {
        key: value
        for key, value in updates.items()
        if "\n" not in value and "\r" not in value
    }
    if len(require_single_line) != len(updates):
        raise ValueError("dotenv values must not contain newlines")

    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    remaining = dict(updates)
    rendered: list[str] = []
    for line in lines:
        match = ENV_ASSIGNMENT.match(line)
        key = match.group("key") if match else None
        if key in updates:
            rendered.append(f"{key}={json.dumps(updates[key], ensure_ascii=False)}")
            remaining.pop(key, None)
        else:
            rendered.append(line)

    if remaining:
        if rendered and rendered[-1]:
            rendered.append("")
        rendered.append("# Test user saved by test/user_registration.py")
        rendered.extend(
            f"{key}={json.dumps(value, ensure_ascii=False)}"
            for key, value in remaining.items()
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text("\n".join(rendered) + "\n", encoding="utf-8")
        if path.exists():
            temporary.chmod(path.stat().st_mode)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
