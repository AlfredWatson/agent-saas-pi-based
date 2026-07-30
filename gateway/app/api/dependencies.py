from uuid import UUID

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.security import decode_token
from ..db.models import User
from ..db.session import get_db

bearer = HTTPBearer()


async def current_user(credentials: HTTPAuthorizationCredentials = Depends(bearer), db: AsyncSession = Depends(get_db)) -> User:
    try:
        user_id = UUID(decode_token(credentials.credentials))
    except Exception as exc:
        raise HTTPException(401, "invalid_token") from exc
    user = await db.scalar(select(User).where(User.id == user_id, User.status == "active"))
    if user is None:
        raise HTTPException(401, "invalid_token")
    return user
