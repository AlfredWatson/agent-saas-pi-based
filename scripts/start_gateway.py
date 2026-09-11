"""Start the Gateway using the listener configured in the repository .env file."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "gateway"))

from app.core.config import get_settings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", help="override GATEWAY_HOST")
    parser.add_argument("--port", type=int, help="override GATEWAY_PORT")
    parser.add_argument("--reload", action="store_true", help="enable Uvicorn autoreload")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=args.host or settings.gateway_host,
        port=args.port or settings.gateway_port,
        reload=args.reload,
        app_dir=str(REPOSITORY_ROOT / "gateway"),
    )


if __name__ == "__main__":
    main()
