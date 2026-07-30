from uuid import UUID
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import current_user
from ...db.models import AgentProfile, ProviderBinding, User
from ...db.session import get_db

router = APIRouter(prefix="/agent-profiles", tags=["profiles"])

class ProfileInput(BaseModel):
    name: str
    provider_binding_id: UUID
    model_id: str
    thinking_level: str | None = None

@router.post("", status_code=201)
async def create_profile(body: ProfileInput, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    binding = await db.scalar(select(ProviderBinding).where(ProviderBinding.id == body.provider_binding_id, ProviderBinding.user_id == user.id, ProviderBinding.status == "active"))
    if binding is None: raise HTTPException(422, "invalid_binding")
    profile = AgentProfile(user_id=user.id, **body.model_dump())
    db.add(profile); await db.commit()
    return {"id": str(profile.id), "name": profile.name, "model_id": profile.model_id}

@router.put("/{profile_id}")
async def update_profile(profile_id: UUID, body: ProfileInput, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    profile = await db.scalar(select(AgentProfile).where(AgentProfile.id == profile_id, AgentProfile.user_id == user.id))
    if profile is None: raise HTTPException(404, "profile_not_found")
    for key, value in body.model_dump().items(): setattr(profile, key, value)
    await db.commit()
    return {"id": str(profile.id), "name": profile.name}
