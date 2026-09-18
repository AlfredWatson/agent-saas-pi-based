from uuid import UUID
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import current_user
from ...db.models import AgentProfile, AgentSession, ProviderBinding, User
from ...db.session import get_db
from ...clients.agent_runtime import RuntimeClient

router = APIRouter(prefix="/agent-profiles", tags=["profiles"])


class ProfileInput(BaseModel):
    name: str
    provider_binding_id: UUID
    model_id: str
    thinking_level: str | None = None


@router.post("", status_code=201)
async def create_profile(
    body: ProfileInput,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    binding = await db.scalar(
        select(ProviderBinding).where(
            ProviderBinding.id == body.provider_binding_id,
            ProviderBinding.user_id == user.id,
            ProviderBinding.status == "active",
        )
    )
    if binding is None:
        raise HTTPException(422, "invalid_binding")
    models = await RuntimeClient().models(str(user.id), binding.provider_id)
    selected = next((item for item in models if item["id"] == body.model_id), None)
    if selected is None:
        raise HTTPException(422, "invalid_model")
    if body.thinking_level and body.thinking_level not in selected.get(
        "thinking_levels", []
    ):
        raise HTTPException(422, "invalid_thinking_level")
    profile = AgentProfile(user_id=user.id, **body.model_dump())
    db.add(profile)
    await db.commit()
    return {"id": str(profile.id), "name": profile.name, "model_id": profile.model_id}


@router.get("")
async def list_profiles(
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    rows = (
        await db.scalars(
            select(AgentProfile)
            .where(AgentProfile.user_id == user.id)
            .order_by(AgentProfile.created_at.desc())
        )
    ).all()
    return {
        "items": [
            {
                "id": str(item.id),
                "name": item.name,
                "model_id": item.model_id,
                "thinking_level": item.thinking_level,
            }
            for item in rows
        ]
    }


@router.put("/{profile_id}")
async def update_profile(
    profile_id: UUID,
    body: ProfileInput,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    profile = await db.scalar(
        select(AgentProfile).where(
            AgentProfile.id == profile_id, AgentProfile.user_id == user.id
        )
    )
    if profile is None:
        raise HTTPException(404, "profile_not_found")
    binding = await db.scalar(
        select(ProviderBinding).where(
            ProviderBinding.id == body.provider_binding_id,
            ProviderBinding.user_id == user.id,
            ProviderBinding.status == "active",
        )
    )
    if binding is None:
        raise HTTPException(422, "invalid_binding")
    models = await RuntimeClient().models(str(user.id), binding.provider_id)
    selected = next((item for item in models if item["id"] == body.model_id), None)
    if selected is None or (
        body.thinking_level
        and body.thinking_level not in selected.get("thinking_levels", [])
    ):
        raise HTTPException(422, "invalid_model_or_thinking")
    for key, value in body.model_dump().items():
        setattr(profile, key, value)
    await db.commit()
    return {"id": str(profile.id), "name": profile.name}


@router.delete("/{profile_id}", status_code=204)
async def delete_profile(
    profile_id: UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    profile = await db.scalar(
        select(AgentProfile).where(
            AgentProfile.id == profile_id, AgentProfile.user_id == user.id
        )
    )
    if profile is None:
        raise HTTPException(404, "profile_not_found")
    session_id = await db.scalar(
        select(AgentSession.id).where(AgentSession.profile_id == profile_id).limit(1)
    )
    if session_id is not None:
        raise HTTPException(409, "profile_in_use")
    await db.delete(profile)
    await db.commit()
