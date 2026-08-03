import os
import stat
from pathlib import Path
from uuid import UUID

from ..core.config import Settings


def workspace_directory(settings: Settings, user_id: UUID, storage_key: str) -> Path:
    root = (settings.resolved_runtime_data_host_root / "tenants" / str(user_id) / "workspaces").resolve()
    path = (root / storage_key).resolve()
    if path.parent != root:
        raise ValueError("invalid_workspace_key")
    return path


def workspace_usage_bytes(settings: Settings, user_id: UUID) -> int:
    root = settings.resolved_runtime_data_host_root / "tenants" / str(user_id) / "workspaces"
    if not root.is_dir():
        return 0
    total = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        entry_stat = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if stat.S_ISREG(entry_stat.st_mode):
                        total += entry_stat.st_size
                    elif stat.S_ISDIR(entry_stat.st_mode):
                        pending.append(Path(entry.path))
        except FileNotFoundError:
            continue
    return total
