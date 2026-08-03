import asyncio
import json
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import current_user
from ...clients.agent_runtime import RuntimeClient
from ...core.encryption import decrypt
from ...db.models import AgentProfile, AgentRun, AgentSession, ChatMessage, ProviderBinding, User, Workspace
from ...db.session import SessionLocal, get_db
from ...services.tool_payloads import safe_tool_content, safe_tool_payload
from ...core.config import get_settings

router = APIRouter(prefix="/sessions", tags=["sessions"])
_subscribers: dict[UUID, set[asyncio.Queue[tuple[str, dict]]]] = {}

class SessionInput(BaseModel): profile_id: UUID; workspace_id: UUID
class MessageInput(BaseModel): content: str = Field(min_length=1, max_length=100_000)

async def owned(session_id: UUID, user: User, db: AsyncSession) -> AgentSession:
    session = await db.scalar(select(AgentSession).where(AgentSession.id == session_id, AgentSession.user_id == user.id))
    if session is None: raise HTTPException(404, "session_not_found")
    return session

async def runtime_payload(db: AsyncSession, session: AgentSession, user_id: UUID) -> dict:
    profile = await db.scalar(select(AgentProfile).where(AgentProfile.id == session.profile_id, AgentProfile.user_id == user_id))
    workspace = await db.scalar(select(Workspace).where(Workspace.id == session.workspace_id, Workspace.user_id == user_id))
    if not profile or not workspace: raise HTTPException(422, "invalid_session_configuration")
    binding = await db.scalar(select(ProviderBinding).where(ProviderBinding.id == profile.provider_binding_id, ProviderBinding.user_id == user_id, ProviderBinding.status == "active"))
    if not binding: raise HTTPException(422, "invalid_binding")
    return {"workspace_key": workspace.storage_key, "model_id": profile.model_id, "thinking_level": profile.thinking_level, "provider_id": binding.provider_id, "api_key": decrypt(binding.ciphertext, binding.nonce, f"{user_id}:{binding.id}:{binding.provider_id}".encode()), "session_file_key": session.pi_session_file_key}

async def publish(run_id: UUID, name: str, data: dict) -> None:
    for queue in list(_subscribers.get(run_id, set())):
        queue.put_nowait((name, data))


def public_tool_event(event: dict, secret_values: tuple[str, ...]) -> tuple[str, dict] | None:
    """Keep the public SSE names stable while extending their safe payloads."""
    kind = event.get("type")
    if kind == "tool_started":
        args, truncated = safe_tool_payload(event.get("args"), secret_values)
        return "tool.started", {
            "tool": event.get("toolName"), "toolCallId": event.get("toolCallId"), "toolName": event.get("toolName"),
            "args": args, "payload_truncated": bool(event.get("payload_truncated") or truncated),
        }
    if kind == "tool_completed":
        result, truncated = safe_tool_payload(event.get("result"), secret_values)
        return "tool.completed", {
            "tool": event.get("toolName"), "toolCallId": event.get("toolCallId"), "toolName": event.get("toolName"),
            "result": result, "isError": bool(event.get("isError")),
            "payload_truncated": bool(event.get("payload_truncated") or truncated),
        }
    return None


async def project_message_end(
    db: AsyncSession,
    session_id: UUID,
    run_id: UUID,
    message: object,
    sequence: int,
    secret_values: tuple[str, ...],
) -> int:
    """Persist only the Runtime's safe Pi message_end projection in Pi order."""
    if not isinstance(message, dict):
        return sequence
    role = message.get("role")
    if role == "assistant":
        blocks = message.get("content")
        if not isinstance(blocks, list):
            return sequence
        emitted_assistant = False
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                content = safe_tool_content(block.get("content"), "[empty assistant text]", secret_values)
                sequence += 1
                db.add(ChatMessage(session_id=session_id, run_id=run_id, role="assistant", sequence=sequence, content=content))
                emitted_assistant = True
            elif block.get("type") == "tool_call":
                # Pi may emit an assistant message containing only tool calls.
                # Keep a non-empty assistant projection for old clients before
                # normalizing each call into its own structured row.
                if not emitted_assistant:
                    sequence += 1
                    db.add(ChatMessage(session_id=session_id, run_id=run_id, role="assistant", sequence=sequence, content="[tool calls]"))
                    emitted_assistant = True
                arguments, truncated = safe_tool_payload(block.get("args"), secret_values)
                tool_name = safe_tool_content(block.get("tool_name"), "unknown", secret_values)
                sequence += 1
                db.add(ChatMessage(
                    session_id=session_id, run_id=run_id, role="tool_call", sequence=sequence, content=tool_name,
                    tool_call_id=safe_tool_content(block.get("tool_call_id"), "unknown", secret_values), tool_name=tool_name,
                    arguments=arguments, payload_truncated=bool(block.get("payload_truncated") or truncated),
                ))
        return sequence
    if role == "tool_result":
        result, truncated = safe_tool_payload(message.get("result"), secret_values)
        tool_name = safe_tool_content(message.get("tool_name"), "unknown", secret_values)
        sequence += 1
        db.add(ChatMessage(
            session_id=session_id, run_id=run_id, role="tool_result", sequence=sequence,
            content=safe_tool_content(message.get("content"), "[tool result]", secret_values),
            tool_call_id=safe_tool_content(message.get("tool_call_id"), "unknown", secret_values), tool_name=tool_name,
            result=result, is_error=bool(message.get("is_error")),
            payload_truncated=bool(message.get("payload_truncated") or truncated),
        ))
    return sequence

async def consume_run(run_id: UUID, session_id: UUID, user_id: UUID, content: str) -> None:
    """Continues after SSE disconnect; Pi ``message_end`` is the history order."""
    settled = False
    try:
        async with SessionLocal() as db:
            session = await db.scalar(select(AgentSession).where(AgentSession.id == session_id, AgentSession.user_id == user_id))
            if not session: return
            runtime = await runtime_payload(db, session, user_id)
            # Defense in depth: Runtime already redacts, but this boundary also
            # knows both credentials and must never persist them if it regresses.
            secret_values = (get_settings().runtime_shared_secret, runtime["api_key"])
            ensured = await RuntimeClient().ensure_session(str(user_id), str(session_id), runtime)
            session.pi_session_id = ensured.get("pi_session_id")
            session.pi_session_file_key = ensured.get("session_file_key")
            sequence = await db.scalar(select(func.coalesce(func.max(ChatMessage.sequence), 0)).where(ChatMessage.session_id == session_id))
            async for event in RuntimeClient().stream_chat(str(user_id), str(session_id), content):
                kind = event.get("type")
                if kind == "text_delta":
                    delta = event.get("delta", "")
                    await publish(run_id, "assistant.delta", {"delta": delta})
                elif kind in {"tool_started", "tool_completed"}:
                    public = public_tool_event(event, secret_values)
                    if public:
                        await publish(run_id, *public)
                elif kind == "message_end":
                    sequence = await project_message_end(db, session_id, run_id, event.get("message"), sequence, secret_values)
                    # This preserves completed calls/results even if a later Pi
                    # cycle fails.  The partial-running unique index prevents a
                    # competing Chat from interleaving sequence values.
                    await db.commit()
                elif kind == "agent_settled":
                    settled = True
                    break
                elif kind == "error": raise RuntimeError("runtime_stream_failed")
            run = await db.get(AgentRun, run_id)
            if not settled: raise RuntimeError("runtime_stream_ended_before_agent_settled")
            if run: run.status, run.finished_at = "completed", datetime.now(UTC)
            binding = await db.scalar(select(ProviderBinding).join(AgentProfile, AgentProfile.provider_binding_id == ProviderBinding.id).where(AgentProfile.id == session.profile_id))
            if binding and binding.verified_at is None: binding.verified_at = datetime.now(UTC)
            await db.commit()
            await publish(run_id, "message.completed", {})
    except Exception:
        async with SessionLocal() as db:
            run = await db.get(AgentRun, run_id)
            if run: run.status, run.error, run.finished_at = "failed", "runtime_stream_failed", datetime.now(UTC); await db.commit()
        await publish(run_id, "message.failed", {"error": "runtime_stream_failed"})
    finally:
        await publish(run_id, "done", {})

@router.post("", status_code=201)
async def create_session(body: SessionInput, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    profile = await db.scalar(select(AgentProfile).where(AgentProfile.id == body.profile_id, AgentProfile.user_id == user.id))
    workspace = await db.scalar(select(Workspace).where(Workspace.id == body.workspace_id, Workspace.user_id == user.id))
    if profile is None or workspace is None: raise HTTPException(422, "invalid_profile_or_workspace")
    session = AgentSession(user_id=user.id, profile_id=profile.id, workspace_id=workspace.id)
    db.add(session); await db.commit()
    # Runtime creation is lazy; this avoids passing a key until an actual chat.
    return {"id": str(session.id), "status": session.status}

@router.get("")
async def list_sessions(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.scalars(select(AgentSession).where(AgentSession.user_id == user.id))).all()
    return {"items": [{"id": str(x.id), "status": x.status, "title": x.title} for x in rows]}

@router.get("/{session_id}/messages")
async def messages(session_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    await owned(session_id, user, db)
    rows = (await db.scalars(select(ChatMessage).where(ChatMessage.session_id == session_id).order_by(ChatMessage.sequence))).all()
    return {"items": [{
        "id": str(x.id), "run_id": str(x.run_id) if x.run_id else None, "role": x.role, "content": x.content,
        "sequence": x.sequence, "status": x.status, "created_at": x.created_at,
        "tool_call_id": x.tool_call_id, "tool_name": x.tool_name, "arguments": x.arguments,
        "result": x.result, "is_error": x.is_error, "payload_truncated": x.payload_truncated,
    } for x in rows]}

@router.post("/{session_id}/messages:stream")
async def stream(session_id: UUID, body: MessageInput, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    await owned(session_id, user, db)
    sequence = await db.scalar(select(func.coalesce(func.max(ChatMessage.sequence), 0)).where(ChatMessage.session_id == session_id))
    run = AgentRun(session_id=session_id, user_id=user.id)
    db.add(run); await db.flush()
    db.add(ChatMessage(session_id=session_id, run_id=run.id, role="user", sequence=sequence + 1, content=body.content))
    try: await db.commit()
    except IntegrityError as exc:
        await db.rollback(); raise HTTPException(409, "session_busy") from exc
    queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()
    _subscribers.setdefault(run.id, set()).add(queue)
    task = asyncio.create_task(consume_run(run.id, session_id, user.id, body.content))
    async def events():
        yield "event: message.accepted\ndata: {}\n\n"
        try:
            while True:
                name, data = await queue.get()
                yield f"event: {name}\ndata: {json.dumps(data)}\n\n"
                if name == "done": return
        finally:
            _subscribers.get(run.id, set()).discard(queue)
            if not _subscribers.get(run.id): _subscribers.pop(run.id, None)
    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

@router.post("/{session_id}/{action}", status_code=202)
async def control(session_id: UUID, action: str, body: MessageInput | None = None, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    if action not in {"abort", "steer", "follow-up"}: raise HTTPException(404, "not_found")
    await owned(session_id, user, db); await RuntimeClient().control(str(user.id), str(session_id), action, body.content if body else None)
    return {"status": "accepted"}
