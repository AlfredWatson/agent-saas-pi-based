from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import current_user
from ...db.models import User, Workspace
from ...db.session import get_db

router = APIRouter(prefix="/workspaces", tags=["workspaces"])

@router.get("")
async def list_workspaces(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.scalars(select(Workspace).where(Workspace.user_id == user.id).order_by(Workspace.created_at))).all()
    return {"items": [{"id": str(item.id), "name": item.name, "status": item.status} for item in rows]}
