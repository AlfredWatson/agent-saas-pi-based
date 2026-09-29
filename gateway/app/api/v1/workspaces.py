import asyncio
import unicodedata
from pathlib import PurePosixPath
from urllib.parse import quote
from uuid import uuid4
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import current_user
from ...clients.agent_runtime import (
    RuntimeClient,
    RuntimeWorkspaceBusyError,
    RuntimeWorkspaceFileError,
)
from ...core.config import get_settings
from ...db.models import AgentProfile, AgentRun, AgentSession, ChatMessage, ProviderBinding, User, Workspace
from ...db.session import get_db
from ...services.runtime_locator import RuntimeUnavailableError
from ...services.workspace_storage import workspace_usage_bytes
from ...services.rag.lifecycle import delete_workspace_rag_data

router = APIRouter(prefix="/workspaces", tags=["workspaces"])

DEFAULT_WORKSPACE_KEY = "default"


class WorkspaceInput(BaseModel):
    name: str


def normalized_workspace_name(value: str) -> str:
    """A display name is never used as a filesystem identifier."""
    name = unicodedata.normalize("NFKC", value).strip()
    if not name or len(name) > 128 or any(unicodedata.category(char).startswith("C") for char in name):
        raise HTTPException(422, "invalid_workspace_name")
    return name


def render(workspace: Workspace) -> dict:
    return {
        "id": str(workspace.id),
        "name": workspace.name,
        "status": workspace.status,
        "is_current": workspace.is_current,
    }


async def owned_workspace(
    db: AsyncSession, workspace_id: UUID, user_id: UUID, *, lock: bool = False
) -> Workspace:
    statement = select(Workspace).where(
        Workspace.id == workspace_id, Workspace.user_id == user_id
    )
    if lock:
        statement = statement.with_for_update()
    workspace = await db.scalar(statement)
    if workspace is None:
        raise HTTPException(404, "workspace_not_found")
    return workspace


async def current_workspace(
    db: AsyncSession, user_id: UUID, *, lock: bool = False
) -> Workspace | None:
    statement = select(Workspace).where(
        Workspace.user_id == user_id, Workspace.is_current, Workspace.status == "active"
    )
    if lock:
        statement = statement.with_for_update()
    return await db.scalar(statement)


async def enforce_workspace_storage_limit(user_id: UUID) -> None:
    settings = get_settings()
    used = await asyncio.to_thread(workspace_usage_bytes, settings, user_id)
    if used >= settings.workspace_storage_limit_mb * 1024 * 1024:
        raise HTTPException(409, "workspace_storage_limit_reached")


async def file_mutation_workspace(
    db: AsyncSession, workspace_id: UUID, user_id: UUID
) -> Workspace:
    # The user lock serializes aggregate-quota mutations across Workspaces.  The
    # Workspace lock is also taken by Chat before it creates an AgentRun, so a
    # Run cannot begin between the busy check and the Runtime file operation.
    await db.scalar(select(User).where(User.id == user_id).with_for_update())
    workspace = await owned_workspace(db, workspace_id, user_id, lock=True)
    if workspace.status != "active":
        raise HTTPException(409, "workspace_unavailable")
    running = await db.scalar(
        select(AgentRun.id)
        .join(AgentSession, AgentSession.id == AgentRun.session_id)
        .where(AgentSession.workspace_id == workspace.id, AgentRun.status == "running")
        .limit(1)
    )
    if running is not None:
        raise HTTPException(409, "workspace_busy")
    return workspace


@router.post("", status_code=201)
async def create_workspace(
    body: WorkspaceInput,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    name = normalized_workspace_name(body.name)
    await enforce_workspace_storage_limit(user.id)
    await db.scalar(select(User).where(User.id == user.id).with_for_update())
    settings = get_settings()
    count = await db.scalar(
        select(func.count()).select_from(Workspace).where(Workspace.user_id == user.id)
    )
    if (
        settings.workspace_max_per_user is not None
        and count >= settings.workspace_max_per_user
    ):
        raise HTTPException(409, "workspace_limit_reached")
    if await db.scalar(
        select(Workspace.id).where(
            Workspace.user_id == user.id, Workspace.name == name
        )
    ):
        raise HTTPException(409, "workspace_exists")
    workspace = Workspace(
        user_id=user.id,
        name=name,
        # Keep the Runtime path independent from a human-readable (and Unicode)
        # display name.  The prefix also makes project-owned directories clear.
        storage_key=f"ws-{uuid4().hex}",
    )
    db.add(workspace)
    await db.commit()
    return render(workspace)


@router.get("")
async def list_workspaces(
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    rows = (
        await db.scalars(
            select(Workspace)
            .where(Workspace.user_id == user.id)
            .order_by(Workspace.created_at)
        )
    ).all()
    return {"items": [render(item) for item in rows]}


@router.post("/{workspace_id}:switch")
async def switch_workspace(
    workspace_id: UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    workspace = await owned_workspace(db, workspace_id, user.id, lock=True)
    if workspace.status != "active":
        raise HTTPException(409, "workspace_unavailable")
    await db.execute(
        update(Workspace)
        .where(Workspace.user_id == user.id, Workspace.is_current)
        .values(is_current=False)
    )
    workspace.is_current = True
    await db.commit()
    return render(workspace)


@router.get("/{workspace_id}/sessions")
async def list_workspace_sessions(
    workspace_id: UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    await owned_workspace(db, workspace_id, user.id)
    rows = (
        await db.scalars(
            select(AgentSession.id)
            .where(
                AgentSession.workspace_id == workspace_id,
                AgentSession.user_id == user.id,
                AgentSession.parent_session_id.is_(None),
            )
            .order_by(AgentSession.created_at)
        )
    ).all()
    return {"session_ids": [str(session_id) for session_id in rows]}


@router.get("/{workspace_id}/files")
async def list_workspace_files(
    workspace_id: UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    workspace = await owned_workspace(db, workspace_id, user.id)
    if workspace.status != "active":
        raise HTTPException(409, "workspace_unavailable")
    try:
        return {
            "items": await RuntimeClient().list_workspace_files(
                str(user.id), workspace.storage_key
            )
        }
    except RuntimeWorkspaceFileError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


@router.get("/{workspace_id}/files/content")
async def download_workspace_file(
    workspace_id: UUID,
    path: str,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    workspace = await owned_workspace(db, workspace_id, user.id)
    if workspace.status != "active":
        raise HTTPException(409, "workspace_unavailable")
    try:
        size, content = await RuntimeClient().download_workspace_file(
            str(user.id), workspace.storage_key, path
        )
    except RuntimeWorkspaceFileError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    filename = PurePosixPath(path).name or "download"
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename, safe='')}",
    }
    if size is not None:
        headers["Content-Length"] = str(size)
    return StreamingResponse(
        content, media_type="application/octet-stream", headers=headers
    )


@router.post("/{workspace_id}/files", status_code=201)
async def upload_workspace_file(
    workspace_id: UUID,
    response: Response,
    path: str = Form(),
    file: UploadFile = File(),
    overwrite: bool = Form(default=False),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        workspace = await file_mutation_workspace(db, workspace_id, user.id)

        async def chunks():
            while chunk := await file.read(64 * 1024):
                yield chunk

        result = await RuntimeClient().upload_workspace_file(
            str(user.id), workspace.storage_key, path, overwrite, chunks()
        )
        await db.commit()
        if not result["created"]:
            response.status_code = 200
        return {key: value for key, value in result.items() if key != "created"}
    except RuntimeWorkspaceFileError as exc:
        await db.rollback()
        raise HTTPException(exc.status_code, exc.detail) from exc
    except RuntimeUnavailableError:
        await db.rollback()
        raise
    except HTTPException:
        await db.rollback()
        raise
    finally:
        await file.close()


@router.delete("/{workspace_id}/files", status_code=204)
async def delete_workspace_file(
    workspace_id: UUID,
    path: str,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        workspace = await file_mutation_workspace(db, workspace_id, user.id)
        await RuntimeClient().delete_workspace_file(
            str(user.id), workspace.storage_key, path
        )
        await db.commit()
    except RuntimeWorkspaceFileError as exc:
        await db.rollback()
        raise HTTPException(exc.status_code, exc.detail) from exc
    except RuntimeUnavailableError:
        await db.rollback()
        raise
    except HTTPException:
        await db.rollback()
        raise


@router.delete("/{workspace_id}", status_code=204)
async def delete_workspace(
    workspace_id: UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    workspace = await owned_workspace(db, workspace_id, user.id, lock=True)
    if workspace.storage_key == DEFAULT_WORKSPACE_KEY:
        raise HTTPException(409, "default_workspace_not_deletable")
    running = await db.scalar(
        select(AgentRun.id)
        .join(AgentSession, AgentSession.id == AgentRun.session_id)
        .where(AgentSession.workspace_id == workspace.id, AgentRun.status == "running")
        .limit(1)
    )
    if running is not None:
        raise HTTPException(409, "workspace_busy")
    workspace.status = "deleting"
    await db.commit()

    sessions = (
        await db.execute(
            select(AgentSession.id, AgentSession.pi_session_file_key).where(
                AgentSession.workspace_id == workspace.id,
                AgentSession.user_id == user.id,
            )
        )
    ).all()
    runtime_sessions = [
        {"session_id": str(session_id), "session_file_key": session_file_key}
        for session_id, session_file_key in sessions
    ]
    try:
        await RuntimeClient().delete_workspace(
            str(user.id), workspace.storage_key, runtime_sessions
        )
    except RuntimeWorkspaceBusyError as exc:
        workspace.status = "active"
        await db.commit()
        raise HTTPException(409, "workspace_busy") from exc
    except RuntimeUnavailableError as exc:
        raise HTTPException(503, "workspace_delete_incomplete") from exc

    workspace = await owned_workspace(db, workspace_id, user.id, lock=True)
    session_ids = [session_id for session_id, _ in sessions]
    if session_ids:
        await db.execute(
            delete(ChatMessage).where(ChatMessage.session_id.in_(session_ids))
        )
        child_ids = list((await db.scalars(select(AgentSession.id).where(
            AgentSession.id.in_(session_ids), AgentSession.parent_session_id.is_not(None)
        ))).all())
        if child_ids:
            await db.execute(delete(AgentRun).where(AgentRun.session_id.in_(child_ids)))
            await db.execute(delete(AgentSession).where(AgentSession.id.in_(child_ids)))
        child_set = set(child_ids)
        parent_ids = [session_id for session_id in session_ids if session_id not in child_set]
        await db.execute(delete(AgentRun).where(AgentRun.session_id.in_(parent_ids)))
        await db.execute(delete(AgentSession).where(AgentSession.id.in_(parent_ids)))
    # Bindings are Workspace-owned.  Delete legacy Profiles first because they
    # retain a Binding foreign key even though the workbench no longer uses one.
    await db.execute(delete(AgentProfile).where(AgentProfile.workspace_id == workspace.id))
    await db.execute(delete(ProviderBinding).where(ProviderBinding.workspace_id == workspace.id))
    await delete_workspace_rag_data(db, workspace.id, user.id)
    if workspace.is_current:
        default = await db.scalar(
            select(Workspace)
            .where(
                Workspace.user_id == user.id,
                Workspace.storage_key == DEFAULT_WORKSPACE_KEY,
            )
            .with_for_update()
        )
        if default is None:
            raise RuntimeError("default_workspace_missing")
        await db.execute(
            update(Workspace)
            .where(Workspace.id == workspace.id)
            .values(is_current=False)
        )
        workspace.is_current = False
        await db.flush()
        default.is_current = True
    await db.delete(workspace)
    await db.commit()
