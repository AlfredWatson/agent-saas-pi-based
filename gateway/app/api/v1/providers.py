"""Workspace-scoped Provider credentials and model discovery."""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import current_user
from ...clients.agent_runtime import RuntimeClient
from ...core.encryption import encrypt
from ...db.models import ProviderBinding, User, Workspace
from ...db.session import get_db
from .workspaces import current_workspace, owned_workspace

router = APIRouter(tags=["providers"])


class BindingInput(BaseModel):
    provider_id: str
    display_name: str
    api_key: str


def render_binding(binding: ProviderBinding) -> dict:
    return {
        "id": str(binding.id),
        "workspace_id": str(binding.workspace_id),
        "provider_id": binding.provider_id,
        "display_name": binding.display_name,
        "status": binding.status,
    }


async def binding_in_workspace(
    db: AsyncSession, workspace_id: UUID, binding_id: UUID, user_id: UUID,
    *, active: bool = False,
) -> ProviderBinding:
    filters = [
        ProviderBinding.id == binding_id,
        ProviderBinding.workspace_id == workspace_id,
        ProviderBinding.user_id == user_id,
    ]
    if active:
        filters.append(ProviderBinding.status == "active")
    binding = await db.scalar(select(ProviderBinding).where(*filters))
    if binding is None:
        raise HTTPException(404, "binding_not_found")
    return binding


async def create_binding(
    workspace: Workspace, body: BindingInput, user: User, db: AsyncSession
) -> dict:
    name = body.display_name.strip()
    if not name or len(name) > 128:
        raise HTTPException(422, "invalid_binding_name")
    await RuntimeClient().accept_provider_binding(str(user.id), body.provider_id, body.api_key)
    binding = ProviderBinding(
        user_id=user.id,
        workspace_id=workspace.id,
        provider_id=body.provider_id,
        display_name=name,
        ciphertext=b"",
        nonce=b"",
    )
    db.add(binding)
    await db.flush()
    binding.ciphertext, binding.nonce = encrypt(
        body.api_key, f"{user.id}:{binding.id}:{binding.provider_id}".encode()
    )
    await db.commit()
    await db.refresh(binding)
    return render_binding(binding)


@router.get("/providers")
async def providers(user: User = Depends(current_user)):
    return {"providers": await RuntimeClient().providers(str(user.id))}


@router.get("/workspaces/{workspace_id}/provider-bindings")
async def list_workspace_bindings(
    workspace_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    await owned_workspace(db, workspace_id, user.id)
    rows = (await db.scalars(
        select(ProviderBinding).where(
            ProviderBinding.workspace_id == workspace_id,
            ProviderBinding.user_id == user.id,
        ).order_by(ProviderBinding.created_at.desc())
    )).all()
    return {"items": [render_binding(item) for item in rows]}


@router.post("/workspaces/{workspace_id}/provider-bindings", status_code=201)
async def create_workspace_binding(
    workspace_id: UUID, body: BindingInput, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    workspace = await owned_workspace(db, workspace_id, user.id)
    if workspace.status != "active":
        raise HTTPException(409, "workspace_unavailable")
    return await create_binding(workspace, body, user, db)


@router.get("/workspaces/{workspace_id}/provider-bindings/{binding_id}")
async def get_workspace_binding(
    workspace_id: UUID, binding_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    await owned_workspace(db, workspace_id, user.id)
    return render_binding(await binding_in_workspace(db, workspace_id, binding_id, user.id))


@router.get("/workspaces/{workspace_id}/provider-bindings/{binding_id}/models")
async def workspace_binding_models(
    workspace_id: UUID, binding_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    await owned_workspace(db, workspace_id, user.id)
    binding = await binding_in_workspace(db, workspace_id, binding_id, user.id, active=True)
    return {"models": await RuntimeClient().models(str(user.id), binding.provider_id)}


@router.get("/workspaces/{workspace_id}/available-models")
async def available_models(
    workspace_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    await owned_workspace(db, workspace_id, user.id)
    bindings = (await db.scalars(select(ProviderBinding).where(
        ProviderBinding.workspace_id == workspace_id,
        ProviderBinding.user_id == user.id,
        ProviderBinding.status == "active",
    ).order_by(ProviderBinding.display_name, ProviderBinding.id))).all()
    items: list[dict] = []
    for binding in bindings:
        for model in await RuntimeClient().models(str(user.id), binding.provider_id):
            items.append({
                "provider_binding_id": str(binding.id),
                "binding_name": binding.display_name,
                **model,
            })
    return {"items": items}


@router.delete("/workspaces/{workspace_id}/provider-bindings/{binding_id}", status_code=204)
async def disable_workspace_binding(
    workspace_id: UUID, binding_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    await owned_workspace(db, workspace_id, user.id)
    binding = await binding_in_workspace(db, workspace_id, binding_id, user.id)
    binding.status = "disabled"
    await db.commit()


# Compatibility routes target only the current Workspace so credentials cannot
# silently appear in a newly selected Workspace.
@router.get("/provider-bindings")
async def list_bindings(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    workspace = await current_workspace(db, user.id)
    if workspace is None:
        return {"items": []}
    return await list_workspace_bindings(workspace.id, user, db)


@router.post("/provider-bindings", status_code=201)
async def create_current_binding(body: BindingInput, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    workspace = await current_workspace(db, user.id)
    if workspace is None:
        raise HTTPException(409, "workspace_unavailable")
    return await create_binding(workspace, body, user, db)


@router.get("/provider-bindings/{binding_id}/models")
async def binding_models(binding_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    binding = await db.scalar(select(ProviderBinding).where(
        ProviderBinding.id == binding_id,
        ProviderBinding.user_id == user.id,
        ProviderBinding.status == "active",
    ))
    if binding is None:
        raise HTTPException(404, "binding_not_found")
    return {"models": await RuntimeClient().models(str(user.id), binding.provider_id)}


@router.delete("/provider-bindings/{binding_id}", status_code=204)
async def delete_binding(binding_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    binding = await db.scalar(select(ProviderBinding).where(
        ProviderBinding.id == binding_id, ProviderBinding.user_id == user.id
    ))
    if binding is None:
        raise HTTPException(404, "binding_not_found")
    binding.status = "disabled"
    await db.commit()
