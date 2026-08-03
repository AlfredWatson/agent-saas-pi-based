from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.services.workspace_storage import workspace_directory, workspace_usage_bytes


def test_workspace_usage_counts_only_regular_workspace_files(tmp_path):
    user_id = uuid4()
    root = tmp_path / ".runtime-data"
    workspace = root / "tenants" / str(user_id) / "workspaces" / "project-a"
    workspace.mkdir(parents=True)
    (workspace / "one.txt").write_bytes(b"abc")
    nested = workspace / "nested"
    nested.mkdir()
    (nested / "two.txt").write_bytes(b"12345")
    (workspace / "linked.txt").symlink_to(tmp_path / "outside.txt")

    settings = SimpleNamespace(resolved_runtime_data_host_root=root)

    assert workspace_usage_bytes(settings, user_id) == 8
    assert workspace_directory(settings, user_id, "project-a") == workspace


def test_workspace_directory_rejects_an_escaping_key(tmp_path):
    settings = SimpleNamespace(resolved_runtime_data_host_root=Path(tmp_path) / ".runtime-data")

    try:
        workspace_directory(settings, uuid4(), "../outside")
    except ValueError as error:
        assert str(error) == "invalid_workspace_key"
    else:
        raise AssertionError("escaping key was accepted")


def test_workspace_limit_configuration_accepts_unlimited_or_a_positive_integer():
    assert Settings(workspace_max_per_user="unlimited").workspace_max_per_user is None
    assert Settings(workspace_max_per_user=1).workspace_max_per_user == 1
    with pytest.raises(ValidationError):
        Settings(workspace_max_per_user=0)
    with pytest.raises(ValidationError):
        Settings(workspace_storage_limit_mb=0)
