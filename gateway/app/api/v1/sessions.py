import json
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import current_user
from ...clients.agent_runtime import RuntimeClient
from ...db.models import AgentProfile, AgentSession, ChatMessage, User, Workspace
from ...db.session import get_db

router = APIRouter(prefix="/sessions", tags=["sessions"])
class SessionInput(BaseModel): profile_id: UUID; workspace_id: UUID
class MessageInput(BaseModel): content: str

async def owned(session_id: UUID, user: User, db: AsyncSession) -> AgentSession:
    session = await db.scalar(select(AgentSession).where(AgentSession.id == session_id, AgentSession.user_id == user.id))
    if session is None: raise HTTPException(404, "session_not_found")
    return session

@router.post("", status_code=201)
async def create_session(body: SessionInput, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    profile = await db.scalar(select(AgentProfile).where(AgentProfile.id == body.profile_id, AgentProfile.user_id == user.id))
    workspace = await db.scalar(select(Workspace).where(Workspace.id == body.workspace_id, Workspace.user_id == user.id))
    if profile is None or workspace is None: raise HTTPException(422, "invalid_profile_or_workspace")
    session = AgentSession(user_id=user.id, profile_id=profile.id, workspace_id=workspace.id)
    db.add(session); await db.flush()
    response = await RuntimeClient().ensure_session(str(user.id), str(session.id), {"workspace_key": workspace.storage_key, "model_id": profile.model_id, "thinking_level": profile.thinking_level})
    session.pi_session_id = response.get("pi_session_id"); session.pi_session_file_key = response.get("session_file_key")
    await db.commit(); return {"id": str(session.id), "status": session.status}

@router.get("")
async def list_sessions(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.scalars(select(AgentSession).where(AgentSession.user_id == user.id))).all()
    return {"items": [{"id": str(x.id), "status": x.status, "title": x.title} for x in rows]}

@router.get("/{session_id}/messages")
async def messages(session_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    await owned(session_id, user, db)
    rows = (await db.scalars(select(ChatMessage).where(ChatMessage.session_id == session_id).order_by(ChatMessage.sequence))).all()
    return {"items": [{"role": x.role, "content": x.content, "sequence": x.sequence} for x in rows]}

@router.post("/{session_id}/messages:stream")
async def stream(session_id: UUID, body: MessageInput, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    await owned(session_id, user, db)
    max_sequence = await db.scalar(select(func.coalesce(func.max(ChatMessage.sequence), 0)).where(ChatMessage.session_id == session_id))
    db.add(ChatMessage(session_id=session_id, role="user", sequence=max_sequence + 1, content=body.content)); await db.commit()
    async def events():
        output = ""
        yield "event: message.accepted\ndata: {}\n\n"
        try:
            async for event in RuntimeClient().stream_chat(str(user.id), str(session_id), body.content):
                if event.get("type") == "text_delta": output += event.get("delta", ""); yield f"event: assistant.delta\ndata: {json.dumps(event)}\n\n"
                elif event.get("type") in {"tool_started", "tool_completed"}: yield f"event: {event['type'].replace('_', '.')}\ndata: {json.dumps(event)}\n\n"
                elif event.get("type") == "agent_settled":
                    db.add(ChatMessage(session_id=session_id, role="assistant", sequence=max_sequence + 2, content=output)); await db.commit()
                    yield "event: message.completed\ndata: {}\n\nevent: done\ndata: {}\n\n"; return
        except Exception as exc:
            yield f"event: message.failed\ndata: {json.dumps({'error': str(exc)})}\n\nevent: done\ndata: {{}}\n\n"
    return StreamingResponse(events(), media_type="text/event-stream")

@router.post("/{session_id}/{action}", status_code=202)
async def control(session_id: UUID, action: str, body: MessageInput | None = None, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    if action not in {"abort", "steer", "follow-up"}: raise HTTPException(404, "not_found")
    await owned(session_id, user, db); await RuntimeClient().control(str(user.id), str(session_id), action, body.content if body else None)
    return {"status": "accepted"}
