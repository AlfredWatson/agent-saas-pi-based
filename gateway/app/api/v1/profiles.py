"""Legacy Agent Profile API.

The workbench configures Sessions directly.  Profiles remain for older API
clients and always inherit the Workspace of their Provider Binding.
"""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import current_user
from ...clients.agent_runtime import RuntimeClient
from ...db.models import AgentProfile, AgentSession, ProviderBinding, User
from ...db.session import get_db
from .workspaces import current_workspace

router = APIRouter(prefix="/agent-profiles", tags=["profiles"])


class ProfileInput(BaseModel):
    name: str
    provider_binding_id: UUID
    model_id: str
    thinking_level: str | None = None


def render(profile: AgentProfile) -> dict:
    return {
        "id": str(profile.id),
        "workspace_id": str(profile.workspace_id),
        "name": profile.name,
        "provider_binding_id": str(profile.provider_binding_id),
        "model_id": profile.model_id,
        "thinking_level": profile.thinking_level,
    }


async def validate_input(body: ProfileInput, user: User, db: AsyncSession) -> ProviderBinding:
    binding = await db.scalar(select(ProviderBinding).where(
        ProviderBinding.id == body.provider_binding_id,
        ProviderBinding.user_id == user.id,
        ProviderBinding.status == "active",
    ))
    if binding is None:
        raise HTTPException(422, "invalid_binding")
    models = await RuntimeClient().models(str(user.id), binding.provider_id)
    selected = next((item for item in models if item["id"] == body.model_id), None)
    if selected is None:
        raise HTTPException(422, "invalid_model")
    if body.thinking_level and body.thinking_level not in selected.get("thinking_levels", []):
        raise HTTPException(422, "invalid_thinking_level")
    return binding


@router.post("", status_code=201)
async def create_profile(body: ProfileInput, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    binding = await validate_input(body, user, db)
    profile = AgentProfile(user_id=user.id, workspace_id=binding.workspace_id, **body.model_dump())
    db.add(profile)
    await db.commit()
    await db.refresh(profile)
    return render(profile)


@router.get("")
async def list_profiles(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    workspace = await current_workspace(db, user.id)
    if workspace is None:
        return {"items": []}
    rows = (await db.scalars(select(AgentProfile).where(
        AgentProfile.user_id == user.id,
        AgentProfile.workspace_id == workspace.id,
    ).order_by(AgentProfile.created_at.desc()))).all()
    return {"items": [render(item) for item in rows]}


@router.put("/{profile_id}")
async def update_profile(profile_id: UUID, body: ProfileInput, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    profile = await db.scalar(select(AgentProfile).where(AgentProfile.id == profile_id, AgentProfile.user_id == user.id))
    if profile is None:
        raise HTTPException(404, "profile_not_found")
    binding = await validate_input(body, user, db)
    if binding.workspace_id != profile.workspace_id:
        raise HTTPException(422, "profile_workspace_mismatch")
    for key, value in body.model_dump().items():
        setattr(profile, key, value)
    await db.commit()
    await db.refresh(profile)
    return render(profile)


@router.delete("/{profile_id}", status_code=204)
async def delete_profile(profile_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    profile = await db.scalar(select(AgentProfile).where(AgentProfile.id == profile_id, AgentProfile.user_id == user.id))
    if profile is None:
        raise HTTPException(404, "profile_not_found")
    session_id = await db.scalar(select(AgentSession.id).where(AgentSession.profile_id == profile_id).limit(1))
    if session_id is not None:
        raise HTTPException(409, "profile_in_use")
    await db.delete(profile)
    await db.commit()
