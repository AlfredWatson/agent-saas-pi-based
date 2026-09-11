"""Shared Gateway endpoint resolution for local operational scripts."""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "gateway"))

from app.core.config import get_settings


def gateway_url() -> str:
    """Prefer an explicit URL; otherwise compose it from the shared .env config."""
    explicit_url = os.getenv("GATEWAY_URL")
    if explicit_url:
        return explicit_url.rstrip("/")
    settings = get_settings()
    return f"http://{settings.gateway_host}:{settings.gateway_port}"
