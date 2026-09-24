"""Workspace-scoped Provider credentials and model discovery."""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import current_user
from ...clients.agent_runtime import LocalModelServiceError, RuntimeClient
from ...core.encryption import decrypt, encrypt
from ...db.models import AgentSession, ProviderBinding, ProviderBindingModel, User, Workspace
from ...db.session import get_db
from .workspaces import current_workspace, owned_workspace

router = APIRouter(tags=["providers"])
LOCAL_PROVIDERS = {"vllm", "sglang"}


class BindingInput(BaseModel):
    provider_id: str
    # Keep this optional at schema level so all absent, empty, and whitespace
    # values become the same explicit public error below.
    display_name: str | None = None
    api_key: str = Field(default="", max_length=4096)
    base_url: str | None = Field(default=None, max_length=2048)


class LocalModelConfigInput(BaseModel):
    model_id: str = Field(min_length=1, max_length=256)
    context_window: int = Field(gt=1)
    max_tokens: int = Field(gt=0)
    reasoning: bool


def render_local_model(model: ProviderBindingModel, provider_id: str) -> dict:
    return {
        "id": model.model_id, "name": model.name, "provider_id": provider_id,
        "thinking_levels": [], "status": model.status,
        "context_window": model.context_window, "max_tokens": model.max_tokens,
        "reasoning": model.reasoning,
    }


async def binding_models_for(db: AsyncSession, binding: ProviderBinding, user_id: UUID, *, ready_only: bool = False) -> list[dict]:
    if binding.provider_id not in LOCAL_PROVIDERS:
        return await RuntimeClient().models(str(user_id), binding.provider_id)
    statement = select(ProviderBindingModel).where(ProviderBindingModel.binding_id == binding.id)
    if ready_only:
        statement = statement.where(ProviderBindingModel.status == "ready")
    rows = (await db.scalars(statement.order_by(ProviderBindingModel.model_id))).all()
    return [render_local_model(row, binding.provider_id) for row in rows]


def render_binding(binding: ProviderBinding) -> dict:
    result = {
        "id": str(binding.id),
        "workspace_id": str(binding.workspace_id),
        "provider_id": binding.provider_id,
        "display_name": binding.display_name,
        "status": binding.status,
    }
    if getattr(binding, "base_url", None):
        result["base_url"] = binding.base_url
    return result


async def binding_in_workspace(
    db: AsyncSession, workspace_id: UUID, binding_id: UUID, user_id: UUID,
    *, active: bool = False, lock: bool = False,
) -> ProviderBinding:
    filters = [
        ProviderBinding.id == binding_id,
        ProviderBinding.workspace_id == workspace_id,
        ProviderBinding.user_id == user_id,
    ]
    if active:
        filters.append(ProviderBinding.status == "active")
    statement = select(ProviderBinding).where(*filters)
    if lock:
        statement = statement.with_for_update()
    binding = await db.scalar(statement)
    if binding is None:
        raise HTTPException(404, "binding_not_found")
    return binding


async def create_binding(
    workspace: Workspace, body: BindingInput, user: User, db: AsyncSession
) -> dict:
    name = (body.display_name or "").strip()
    if not name or len(name) > 128:
        raise HTTPException(422, "invalid_binding_name")
    discovery = None
    if body.provider_id in LOCAL_PROVIDERS:
        if not body.base_url:
            raise HTTPException(422, "invalid_model_base_url")
        try:
            discovery = await RuntimeClient().discover_local_models(str(user.id), body.base_url, body.api_key)
        except LocalModelServiceError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc
    else:
        if not body.api_key or body.base_url is not None:
            raise HTTPException(422, "invalid_provider")
        await RuntimeClient().accept_provider_binding(str(user.id), body.provider_id, body.api_key)
    binding = ProviderBinding(
        user_id=user.id,
        workspace_id=workspace.id,
        provider_id=body.provider_id,
        display_name=name,
        base_url=discovery["base_url"] if discovery else None,
        ciphertext=b"",
        nonce=b"",
    )
    db.add(binding)
    await db.flush()
    binding.ciphertext, binding.nonce = encrypt(
        body.api_key, f"{user.id}:{binding.id}:{binding.provider_id}".encode()
    )
    if discovery:
        for model in discovery["models"]:
            db.add(ProviderBindingModel(
                binding_id=binding.id, model_id=model["id"], name=model["name"], status="pending"
            ))
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
            ProviderBinding.status == "active",
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
    return {"models": await binding_models_for(db, binding, user.id)}


@router.put("/workspaces/{workspace_id}/provider-bindings/{binding_id}/models:configure")
async def configure_local_model(
    workspace_id: UUID, binding_id: UUID, body: LocalModelConfigInput,
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db),
):
    await owned_workspace(db, workspace_id, user.id)
    binding = await binding_in_workspace(db, workspace_id, binding_id, user.id, active=True, lock=True)
    if binding.provider_id not in LOCAL_PROVIDERS or body.max_tokens >= body.context_window:
        raise HTTPException(422, "invalid_model_config")
    model = await db.scalar(select(ProviderBindingModel).where(
        ProviderBindingModel.binding_id == binding.id,
        ProviderBindingModel.model_id == body.model_id,
    ))
    if model is None:
        raise HTTPException(422, "invalid_model")
    if model.status == "unavailable":
        raise HTTPException(409, "model_unavailable")
    model.context_window = body.context_window
    model.max_tokens = body.max_tokens
    model.reasoning = body.reasoning
    model.status = "ready"
    await db.commit()
    return render_local_model(model, binding.provider_id)


@router.post("/workspaces/{workspace_id}/provider-bindings/{binding_id}/models:refresh")
async def refresh_local_models(
    workspace_id: UUID, binding_id: UUID,
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db),
):
    await owned_workspace(db, workspace_id, user.id)
    binding = await binding_in_workspace(db, workspace_id, binding_id, user.id, active=True, lock=True)
    if binding.provider_id not in LOCAL_PROVIDERS or not binding.base_url:
        raise HTTPException(422, "invalid_provider")
    api_key = decrypt(binding.ciphertext, binding.nonce, f"{user.id}:{binding.id}:{binding.provider_id}".encode())
    try:
        discovery = await RuntimeClient().discover_local_models(str(user.id), binding.base_url, api_key)
    except LocalModelServiceError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    rows = (await db.scalars(select(ProviderBindingModel).where(
        ProviderBindingModel.binding_id == binding.id
    ))).all()
    existing = {row.model_id: row for row in rows}
    seen = set()
    for item in discovery["models"]:
        seen.add(item["id"])
        model = existing.get(item["id"])
        if model is None:
            model = ProviderBindingModel(binding_id=binding.id, model_id=item["id"], name=item["name"], status="pending")
            db.add(model)
        else:
            model.name = item["name"]
            model.status = "ready" if model.context_window is not None else "pending"
    for model in rows:
        if model.model_id not in seen:
            model.status = "unavailable"
    await db.commit()
    return {"models": await binding_models_for(db, binding, user.id)}


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
        for model in await binding_models_for(db, binding, user.id, ready_only=True):
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
    binding = await binding_in_workspace(
        db, workspace_id, binding_id, user.id, lock=True
    )
    if binding.status != "active":
        return
    in_use = await db.scalar(select(AgentSession.id).where(
        AgentSession.workspace_id == workspace_id,
        AgentSession.user_id == user.id,
        AgentSession.provider_binding_id == binding.id,
    ).limit(1))
    if in_use is not None:
        raise HTTPException(409, "provider_binding_in_use")
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
    return {"models": await binding_models_for(db, binding, user.id)}


@router.delete("/provider-bindings/{binding_id}", status_code=204)
async def delete_binding(binding_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    binding = await db.scalar(select(ProviderBinding).where(
        ProviderBinding.id == binding_id, ProviderBinding.user_id == user.id
    ).with_for_update())
    if binding is None:
        raise HTTPException(404, "binding_not_found")
    if binding.status != "active":
        return
    in_use = await db.scalar(select(AgentSession.id).where(
        AgentSession.workspace_id == binding.workspace_id,
        AgentSession.user_id == user.id,
        AgentSession.provider_binding_id == binding.id,
    ).limit(1))
    if in_use is not None:
        raise HTTPException(409, "provider_binding_in_use")
    binding.status = "disabled"
    await db.commit()
