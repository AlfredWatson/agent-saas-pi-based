from pydantic import BaseModel, EmailStr, Field
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import current_user
from ...core.security import create_token, hash_password, verify_password
from ...db.models import User, Workspace
from ...db.session import get_db

router = APIRouter(prefix="/auth", tags=["auth"])

class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=256)

@router.post("/register", status_code=201)
async def register(body: Credentials, db: AsyncSession = Depends(get_db)):
    email = body.email.lower()
    if await db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "email_exists")
    user = User(email=email, password_hash=hash_password(body.password))
    db.add(user)
    await db.flush()
    db.add(Workspace(user_id=user.id, name="default", storage_key="default", is_current=True))
    await db.commit()
    return {"access_token": create_token(str(user.id)), "token_type": "bearer"}

@router.post("/login")
async def login(body: Credentials, db: AsyncSession = Depends(get_db)):
    user = await db.scalar(select(User).where(User.email == body.email.lower()))
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "invalid_credentials")
    return {"access_token": create_token(str(user.id)), "token_type": "bearer"}

@router.get("/me")
async def me(user: User = Depends(current_user)):
    return {"id": str(user.id), "email": user.email, "status": user.status}
