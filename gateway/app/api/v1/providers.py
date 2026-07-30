from uuid import UUID
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import current_user
from ...clients.agent_runtime import RuntimeClient
from ...core.encryption import encrypt
from ...db.models import ProviderBinding, User
from ...db.session import get_db

router = APIRouter(tags=["providers"])

class BindingInput(BaseModel):
    provider_id: str
    display_name: str
    api_key: str

@router.get("/providers")
async def providers(user: User = Depends(current_user)):
    return {"providers": await RuntimeClient().providers(str(user.id))}

@router.post("/provider-bindings", status_code=201)
async def create_binding(body: BindingInput, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    models = await RuntimeClient().validate_provider(str(user.id), body.provider_id, body.api_key)
    binding = ProviderBinding(user_id=user.id, provider_id=body.provider_id, display_name=body.display_name, ciphertext=b"", nonce=b"")
    db.add(binding)
    await db.flush()
    binding.ciphertext, binding.nonce = encrypt(body.api_key, f"{user.id}:{binding.id}:{body.provider_id}".encode())
    await db.commit()
    return {"id": str(binding.id), "models": models}

@router.get("/provider-bindings")
async def list_bindings(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.scalars(select(ProviderBinding).where(ProviderBinding.user_id == user.id))).all()
    return {"items": [{"id": str(x.id), "provider_id": x.provider_id, "display_name": x.display_name, "status": x.status} for x in rows]}

@router.get("/provider-bindings/{binding_id}/models")
async def binding_models(binding_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    binding = await db.scalar(select(ProviderBinding).where(ProviderBinding.id == binding_id, ProviderBinding.user_id == user.id, ProviderBinding.status == "active"))
    if binding is None: raise HTTPException(404, "binding_not_found")
    return {"models": await RuntimeClient().providers(str(user.id))}

@router.delete("/provider-bindings/{binding_id}", status_code=204)
async def delete_binding(binding_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    binding = await db.scalar(select(ProviderBinding).where(ProviderBinding.id == binding_id, ProviderBinding.user_id == user.id))
    if binding is None: raise HTTPException(404, "binding_not_found")
    binding.status = "disabled"; await db.commit()
