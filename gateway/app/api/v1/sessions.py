import asyncio
import json
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import current_user
from ...clients.agent_runtime import (
    RuntimeClient,
    RuntimeSessionBusyError,
)
from ...services.runtime_locator import RuntimeUnavailableError
from ...core.encryption import decrypt
from ...db.models import (
    AgentProfile,
    AgentRun,
    AgentSession,
    AgentSessionKnowledgeBase,
    AgentSubagentDefinition,
    ChatMessage,
    ProviderBinding,
    User,
    Workspace,
)
from ...db.rag.models import KnowledgeBase
from ...db.session import SessionLocal, get_db
from ...services.tool_payloads import safe_tool_content, safe_tool_payload
from ...services.agent_tools import AgentConfigInput, effective_tools, validate_rag_binding
from ...core.config import get_settings
from .workspaces import current_workspace, enforce_workspace_storage_limit

router = APIRouter(prefix="/sessions", tags=["sessions"])
_subscribers: dict[UUID, set[asyncio.Queue[tuple[str, dict]]]] = {}


class SessionInput(AgentConfigInput):
    profile_id: UUID | None = None
    workspace_id: UUID | None = None
    knowledge_base_ids: list[UUID] = Field(default_factory=list, max_length=20)

    @field_validator("knowledge_base_ids")
    @classmethod
    def unique_knowledge_base_ids(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("duplicate_knowledge_base_id")
        return value


class MessageInput(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)


class SessionTitleInput(BaseModel):
    title: str = Field(min_length=1, max_length=256)


class SessionModelConfigInput(BaseModel):
    provider_binding_id: UUID
    model_id: str = Field(min_length=1, max_length=256)
    thinking_level: str | None = Field(default=None, max_length=64)


class AgentConfigUpdateInput(AgentConfigInput):
    expected_config_version: int = Field(ge=1)


async def owned(
    session_id: UUID, user: User, db: AsyncSession, *, lock: bool = False
) -> AgentSession:
    query = select(AgentSession).where(
        AgentSession.id == session_id, AgentSession.user_id == user.id,
        AgentSession.parent_session_id.is_(None),
    )
    if lock:
        query = query.with_for_update()
    session = await db.scalar(query)
    if session is None:
        raise HTTPException(404, "session_not_found")
    return session


def render_run(run: AgentRun | None) -> dict | None:
    if run is None:
        return None
    value = {
        "id": str(run.id),
        "status": run.status,
        "error": run.error,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }
    if hasattr(run, "provider_binding_id"):
        value.update({
            "provider_binding_id": str(run.provider_binding_id) if run.provider_binding_id else None,
            "provider_id": run.provider_id,
            "model_id": run.model_id,
            "thinking_level": run.thinking_level,
        })
    return value


def render_session(
    session: AgentSession, knowledge_base_ids: list[str], latest_run: AgentRun | None
) -> dict:
    return {
        "id": str(session.id),
        "status": session.status,
        "title": session.title,
        "knowledge_base_ids": sorted(knowledge_base_ids),
        "workspace_id": str(session.workspace_id),
        "profile_id": str(session.profile_id) if session.profile_id else None,
        "provider_binding_id": str(getattr(session, "provider_binding_id", None)) if getattr(session, "provider_binding_id", None) else None,
        "model_id": getattr(session, "model_id", None),
        "thinking_level": getattr(session, "thinking_level", None),
        "model_configured": bool(getattr(session, "provider_binding_id", None) and getattr(session, "model_id", None)),
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "latest_run": render_run(latest_run),
        "total_tokens": session.total_tokens,
        "context_tokens": session.context_tokens,
        "tools": getattr(session, "tools", None),
        "config_version": getattr(session, "config_version", 1),
    }


def read_token_snapshot(payload: object) -> tuple[int, int]:
    """Accept only unsigned values representable by PostgreSQL BIGINT."""
    if not isinstance(payload, dict):
        raise ValueError("invalid_session_token_stats")
    values = (payload.get("total_tokens"), payload.get("context_tokens"))
    if any(type(value) is not int or value < 0 or value > 2**63 - 1 for value in values):
        raise ValueError("invalid_session_token_stats")
    return values


async def session_details(
    db: AsyncSession, sessions: list[AgentSession]
) -> tuple[dict[UUID, list[str]], dict[UUID, AgentRun]]:
    """Load UI metadata in bounded queries without changing existing IDs."""
    ids = [item.id for item in sessions]
    if not ids:
        return {}, {}
    bindings = list(
        (
            await db.execute(
                select(
                    AgentSessionKnowledgeBase.session_id,
                    AgentSessionKnowledgeBase.knowledge_base_id,
                ).where(AgentSessionKnowledgeBase.session_id.in_(ids))
            )
        ).all()
    )
    knowledge_bases: dict[UUID, list[str]] = {item.id: [] for item in sessions}
    for session_id, knowledge_base_id in bindings:
        knowledge_bases[session_id].append(str(knowledge_base_id))
    runs = (
        await db.scalars(
            select(AgentRun)
            .where(AgentRun.session_id.in_(ids))
            .order_by(
                AgentRun.session_id, AgentRun.started_at.desc(), AgentRun.id.desc()
            )
        )
    ).all()
    latest_runs: dict[UUID, AgentRun] = {}
    for run in runs:
        latest_runs.setdefault(run.session_id, run)
    return knowledge_bases, latest_runs


async def family_running_run(db: AsyncSession, session_id: UUID) -> UUID | None:
    child_ids = select(AgentSession.id).where(AgentSession.parent_session_id == session_id)
    return await db.scalar(select(AgentRun.id).where(
        (AgentRun.session_id == session_id) | AgentRun.session_id.in_(child_ids),
        AgentRun.status == "running",
    ).limit(1))


async def runtime_payload(
    db: AsyncSession, session: AgentSession, user_id: UUID
) -> dict:
    workspace = await db.scalar(
        select(Workspace).where(
            Workspace.id == session.workspace_id, Workspace.user_id == user_id
        )
    )
    if not workspace or workspace.status != "active":
        raise HTTPException(422, "invalid_session_configuration")
    binding_id = session.provider_binding_id
    model_id = session.model_id
    thinking_level = session.thinking_level
    # Sessions created through the retained Profile API have their config copied
    # on creation. This fallback only protects a partially upgraded database.
    if (binding_id is None or model_id is None) and session.profile_id:
        profile = await db.scalar(select(AgentProfile).where(
            AgentProfile.id == session.profile_id, AgentProfile.user_id == user_id
        ))
        if profile:
            binding_id, model_id, thinking_level = (
                profile.provider_binding_id, profile.model_id, profile.thinking_level
            )
    if binding_id is None or model_id is None:
        raise HTTPException(409, "session_model_not_configured")
    binding = await db.scalar(
        select(ProviderBinding).where(
            ProviderBinding.id == binding_id,
            ProviderBinding.user_id == user_id,
            ProviderBinding.workspace_id == workspace.id,
            ProviderBinding.status == "active",
        )
    )
    if not binding:
        raise HTTPException(422, "invalid_binding")
    local_model_payload = None
    if binding.provider_id in {"vllm", "sglang"}:
        from ...db.models import ProviderBindingModel
        local_model = await db.scalar(select(ProviderBindingModel).where(
            ProviderBindingModel.binding_id == binding.id,
            ProviderBindingModel.model_id == model_id,
        ))
        if local_model is None or local_model.status != "ready":
            raise HTTPException(409, "model_unavailable")
        local_model_payload = {
            "id": local_model.model_id, "name": local_model.name,
            "context_window": local_model.context_window,
            "max_tokens": local_model.max_tokens, "reasoning": local_model.reasoning,
        }
    knowledge_bases = list(
        (
            await db.execute(
                select(KnowledgeBase.id, KnowledgeBase.name)
                .join(
                    AgentSessionKnowledgeBase,
                    AgentSessionKnowledgeBase.knowledge_base_id == KnowledgeBase.id,
                )
                .where(
                    AgentSessionKnowledgeBase.session_id == session.id,
                    KnowledgeBase.user_id == user_id,
                    KnowledgeBase.workspace_id == workspace.id,
                    KnowledgeBase.status == "active",
                )
                .order_by(KnowledgeBase.name, KnowledgeBase.id)
            )
        ).all()
    )
    return {
        "workspace_key": workspace.storage_key,
        "provider_binding_id": str(binding.id),
        "model_id": model_id,
        "thinking_level": thinking_level,
        "provider_id": binding.provider_id,
        "base_url": binding.base_url,
        "local_model": local_model_payload,
        "api_key": decrypt(
            binding.ciphertext,
            binding.nonce,
            f"{user_id}:{binding.id}:{binding.provider_id}".encode(),
        ),
        "session_file_key": session.pi_session_file_key,
        "knowledge_bases": [
            {"id": str(knowledge_base_id), "name": name}
            for knowledge_base_id, name in knowledge_bases
        ],
        "tools": effective_tools(session.tools, bool(knowledge_bases)),
        "subagent_system_prompt": (session.subagent_snapshot or {}).get("system_prompt") if session.parent_session_id else None,
        "subagents": [
            {"name": item.name, "description": item.description, "tools": item.tools}
            for item in (await db.scalars(select(AgentSubagentDefinition).where(
                AgentSubagentDefinition.session_id == session.id
            ).order_by(AgentSubagentDefinition.name))).all()
        ] if session.parent_session_id is None else [],
        "config_version": session.config_version,
    }


async def validate_model_config(
    db: AsyncSession,
    user_id: UUID,
    workspace_id: UUID,
    body: SessionModelConfigInput,
) -> ProviderBinding:
    # Serialize model changes with Binding disablement.  Once this lock has
    # been acquired, a concurrent delete either observes this Session's new
    # reference or this validation sees the disabled Binding.
    binding = await db.scalar(select(ProviderBinding).where(
        ProviderBinding.id == body.provider_binding_id,
        ProviderBinding.user_id == user_id,
        ProviderBinding.workspace_id == workspace_id,
        ProviderBinding.status == "active",
    ).with_for_update())
    if binding is None:
        raise HTTPException(422, "invalid_binding")
    if binding.provider_id in {"vllm", "sglang"}:
        from .providers import binding_models_for
        models = await binding_models_for(db, binding, user_id, ready_only=True)
    else:
        models = await RuntimeClient().models(str(user_id), binding.provider_id)
    model = next((item for item in models if item["id"] == body.model_id), None)
    if model is None:
        if binding.provider_id in {"vllm", "sglang"}:
            from ...db.models import ProviderBindingModel
            stored = await db.scalar(select(ProviderBindingModel).where(
                ProviderBindingModel.binding_id == binding.id,
                ProviderBindingModel.model_id == body.model_id,
            ))
            if stored is not None and stored.status == "unavailable":
                raise HTTPException(409, "model_unavailable")
        raise HTTPException(422, "invalid_model")
    if body.thinking_level and body.thinking_level not in model.get("thinking_levels", []):
        raise HTTPException(422, "invalid_thinking_level")
    return binding


async def publish(run_id: UUID, name: str, data: dict) -> None:
    for queue in list(_subscribers.get(run_id, set())):
        queue.put_nowait((name, data))


def public_tool_event(
    event: dict, secret_values: tuple[str, ...]
) -> tuple[str, dict] | None:
    """Keep the public SSE names stable while extending their safe payloads."""
    kind = event.get("type")
    if kind == "tool_started":
        args, truncated = safe_tool_payload(event.get("args"), secret_values)
        return "tool.started", {
            "tool": event.get("toolName"),
            "toolCallId": event.get("toolCallId"),
            "toolName": event.get("toolName"),
            "args": args,
            "payload_truncated": bool(event.get("payload_truncated") or truncated),
        }
    if kind == "tool_completed":
        result, truncated = safe_tool_payload(event.get("result"), secret_values)
        return "tool.completed", {
            "tool": event.get("toolName"),
            "toolCallId": event.get("toolCallId"),
            "toolName": event.get("toolName"),
            "result": result,
            "isError": bool(event.get("isError")),
            "payload_truncated": bool(event.get("payload_truncated") or truncated),
        }
    return None


def public_compaction_event(event: dict) -> tuple[str, dict] | None:
    """Expose only Pi's compaction reason and outcome on the public stream."""
    reason = event.get("reason")
    if not isinstance(reason, str) or reason not in {"manual", "threshold", "overflow"}:
        return None
    if event.get("type") == "compaction_started":
        return "compaction.started", {"reason": reason}
    if event.get("type") == "compaction_ended":
        status = event.get("status")
        if isinstance(status, str) and status in {"completed", "failed", "aborted"}:
            return "compaction.ended", {"reason": reason, "status": status}
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
                content = safe_tool_content(
                    block.get("content"), "[empty assistant text]", secret_values
                )
                sequence += 1
                db.add(
                    ChatMessage(
                        session_id=session_id,
                        run_id=run_id,
                        role="assistant",
                        sequence=sequence,
                        content=content,
                    )
                )
                emitted_assistant = True
            elif block.get("type") == "tool_call":
                # Pi may emit an assistant message containing only tool calls.
                # Keep a non-empty assistant projection for old clients before
                # normalizing each call into its own structured row.
                if not emitted_assistant:
                    sequence += 1
                    db.add(
                        ChatMessage(
                            session_id=session_id,
                            run_id=run_id,
                            role="assistant",
                            sequence=sequence,
                            content="[tool calls]",
                        )
                    )
                    emitted_assistant = True
                arguments, truncated = safe_tool_payload(
                    block.get("args"), secret_values
                )
                tool_name = safe_tool_content(
                    block.get("tool_name"), "unknown", secret_values
                )
                sequence += 1
                db.add(
                    ChatMessage(
                        session_id=session_id,
                        run_id=run_id,
                        role="tool_call",
                        sequence=sequence,
                        content=tool_name,
                        tool_call_id=safe_tool_content(
                            block.get("tool_call_id"), "unknown", secret_values
                        ),
                        tool_name=tool_name,
                        arguments=arguments,
                        payload_truncated=bool(
                            block.get("payload_truncated") or truncated
                        ),
                    )
                )
        return sequence
    if role == "tool_result":
        result, truncated = safe_tool_payload(message.get("result"), secret_values)
        tool_name = safe_tool_content(
            message.get("tool_name"), "unknown", secret_values
        )
        sequence += 1
        db.add(
            ChatMessage(
                session_id=session_id,
                run_id=run_id,
                role="tool_result",
                sequence=sequence,
                content=safe_tool_content(
                    message.get("content"), "[tool result]", secret_values
                ),
                tool_call_id=safe_tool_content(
                    message.get("tool_call_id"), "unknown", secret_values
                ),
                tool_name=tool_name,
                result=result,
                is_error=bool(message.get("is_error")),
                payload_truncated=bool(message.get("payload_truncated") or truncated),
            )
        )
    return sequence


async def consume_run(
    run_id: UUID, session_id: UUID, user_id: UUID, content: str
) -> None:
    """Continues after SSE disconnect; Pi ``message_end`` is the history order."""
    settled = False
    output_truncated = False
    try:
        async with SessionLocal() as db:
            session = await db.scalar(
                select(AgentSession).where(
                    AgentSession.id == session_id, AgentSession.user_id == user_id
                )
            )
            if not session:
                return
            active_run = await db.get(AgentRun, run_id)
            if active_run is None or active_run.status == "cancelled":
                return
            runtime = await runtime_payload(db, session, user_id)
            # Defense in depth: Runtime already redacts, but this boundary also
            # knows both credentials and must never persist them if it regresses.
            secret_values = (get_settings().runtime_shared_secret, runtime["api_key"])
            ensured = await RuntimeClient().ensure_session(
                str(user_id), str(session_id), runtime
            )
            session.pi_session_id = ensured.get("pi_session_id")
            session.pi_session_file_key = ensured.get("session_file_key")
            session.total_tokens, session.context_tokens = read_token_snapshot(ensured)
            await db.commit()
            await db.refresh(active_run)
            if active_run.status == "cancelled":
                return
            sequence = await db.scalar(
                select(func.coalesce(func.max(ChatMessage.sequence), 0)).where(
                    ChatMessage.session_id == session_id
                )
            )
            async with asyncio.timeout(900 if session.parent_session_id else None):
                async for event in RuntimeClient().stream_chat(
                    str(user_id), str(session_id), content
                ):
                    kind = event.get("type")
                    if kind == "text_delta":
                        delta = event.get("delta", "")
                        await publish(run_id, "assistant.delta", {"delta": delta})
                    elif kind in {"tool_started", "tool_completed"}:
                        public = public_tool_event(event, secret_values)
                        if public:
                            await publish(run_id, *public)
                    elif kind in {"compaction_started", "compaction_ended"}:
                        public = public_compaction_event(event)
                        if public:
                            await publish(run_id, *public)
                    elif kind == "message_end":
                        message = event.get("message")
                        if isinstance(message, dict) and message.get("role") == "assistant":
                            output_truncated = message.get("stop_reason") == "length"
                        sequence = await project_message_end(
                            db,
                            session_id,
                            run_id,
                            message,
                            sequence,
                            secret_values,
                        )
                        # Persist completed calls even if a later Pi cycle fails.
                        await db.commit()
                    elif kind == "token_snapshot":
                        session.total_tokens, session.context_tokens = read_token_snapshot(event)
                        await db.commit()
                    elif kind == "agent_settled":
                        settled = True
                        break
                    elif kind == "error":
                        if session.parent_session_id and event.get("error") == "subagent_timeout":
                            raise TimeoutError("subagent_timeout")
                        raise RuntimeError("runtime_stream_failed")
            run = await db.get(AgentRun, run_id)
            if not settled:
                raise RuntimeError("runtime_stream_ended_before_agent_settled")
            if run:
                await db.refresh(run)
            if run and run.status != "cancelled":
                run.status = "failed" if output_truncated else "completed"
                run.error = "output_token_limit" if output_truncated else None
                run.finished_at = datetime.now(UTC)
            binding = await db.scalar(select(ProviderBinding).where(
                ProviderBinding.id == session.provider_binding_id,
                ProviderBinding.user_id == user_id,
            ))
            if binding and binding.verified_at is None:
                binding.verified_at = datetime.now(UTC)
            await db.commit()
            if output_truncated:
                await publish(run_id, "message.failed", {"error": "output_token_limit"})
            else:
                await publish(run_id, "message.completed", {})
    except TimeoutError:
        try:
            await RuntimeClient().control(str(user_id), str(session_id), "abort")
        except Exception:
            pass
        async with SessionLocal() as db:
            run = await db.get(AgentRun, run_id)
            if run and run.status != "cancelled":
                run.status, run.error, run.finished_at = "failed", "subagent_timeout", datetime.now(UTC)
                await db.commit()
        await publish(run_id, "message.failed", {"error": "subagent_timeout"})
    except Exception:
        async with SessionLocal() as db:
            run = await db.get(AgentRun, run_id)
            if run and run.status != "cancelled":
                run.status, run.error, run.finished_at = (
                    "failed",
                    "runtime_stream_failed",
                    datetime.now(UTC),
                )
                await db.commit()
        await publish(run_id, "message.failed", {"error": "runtime_stream_failed"})
    finally:
        # A parent stream may end after batch creation but before the tool
        # starts every queued child. Never leave those sessions executable.
        async with SessionLocal() as db:
            orphan_runs = list((await db.scalars(select(AgentRun).join(
                AgentSession, AgentSession.id == AgentRun.session_id
            ).where(
                AgentSession.parent_run_id == run_id,
                AgentRun.status.in_(["queued", "running"]),
            ))).all())
            running_children = [item.session_id for item in orphan_runs if item.status == "running"]
            for item in orphan_runs:
                item.status, item.error, item.finished_at = "cancelled", "parent_run_ended", datetime.now(UTC)
            if orphan_runs:
                await db.commit()
        for child_id in running_children:
            try:
                await RuntimeClient().control(str(user_id), str(child_id), "abort")
            except Exception:
                pass
        await publish(run_id, "done", {})


@router.post("", status_code=201)
async def create_session(
    body: SessionInput,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    profile = None
    if body.profile_id is not None:
        profile = await db.scalar(select(AgentProfile).where(
            AgentProfile.id == body.profile_id, AgentProfile.user_id == user.id
        ))
    if body.workspace_id is None:
        workspace = await current_workspace(db, user.id)
    else:
        workspace = await db.scalar(
            select(Workspace).where(
                Workspace.id == body.workspace_id,
                Workspace.user_id == user.id,
                Workspace.status == "active",
            )
        )
    if workspace is None:
        raise HTTPException(422, "invalid_workspace")
    if profile is not None and profile.workspace_id != workspace.id:
        raise HTTPException(422, "invalid_profile_or_workspace")
    if body.profile_id is not None and profile is None:
        raise HTTPException(422, "invalid_profile_or_workspace")
    if body.knowledge_base_ids and get_settings().runtime_gateway_base_url is None:
        raise HTTPException(409, "agent_rag_unavailable")
    if body.tools is not None and "call_subagents" in body.tools and get_settings().runtime_gateway_base_url is None:
        raise HTTPException(409, "agent_subagents_unavailable")
    try:
        validate_rag_binding(body, bool(body.knowledge_base_ids))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    knowledge_bases: list[KnowledgeBase] = []
    if body.knowledge_base_ids:
        knowledge_bases = list(
            (
                await db.scalars(
                    select(KnowledgeBase).where(
                        KnowledgeBase.id.in_(body.knowledge_base_ids),
                        KnowledgeBase.user_id == user.id,
                        KnowledgeBase.workspace_id == workspace.id,
                        KnowledgeBase.status == "active",
                    ).order_by(KnowledgeBase.id).with_for_update()
                )
            ).all()
        )
        if len(knowledge_bases) != len(body.knowledge_base_ids):
            raise HTTPException(422, "invalid_knowledge_base_binding")
    session = AgentSession(
        user_id=user.id,
        profile_id=profile.id if profile else None,
        workspace_id=workspace.id,
        provider_binding_id=profile.provider_binding_id if profile else None,
        model_id=profile.model_id if profile else None,
        thinking_level=profile.thinking_level if profile else None,
        tools=body.tools,
    )
    db.add(session)
    await db.flush()
    db.add_all(
        AgentSessionKnowledgeBase(
            session_id=session.id, knowledge_base_id=knowledge_base_id
        )
        for knowledge_base_id in body.knowledge_base_ids
    )
    if body.subagents:
        db.add_all(AgentSubagentDefinition(
            session_id=session.id, name=item.name, description=item.description,
            system_prompt=item.system_prompt, tools=item.tools,
        ) for item in body.subagents)
    await db.commit()
    # Runtime creation is lazy; this avoids passing a key until an actual chat.
    await db.refresh(session)
    return render_session(
        session,
        [str(item) for item in body.knowledge_base_ids],
        None,
    )


@router.put("/{session_id}/agent-config")
async def set_agent_config(
    session_id: UUID,
    body: AgentConfigUpdateInput,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    session = await owned(session_id, user, db, lock=True)
    if session.config_version != body.expected_config_version:
        raise HTTPException(409, "session_config_conflict")
    if body.tools is not None and "call_subagents" in body.tools and get_settings().runtime_gateway_base_url is None:
        raise HTTPException(409, "agent_subagents_unavailable")
    running = await family_running_run(db, session.id)
    if running is not None:
        raise HTTPException(409, "session_busy")
    has_kb = bool(await db.scalar(select(AgentSessionKnowledgeBase.session_id).where(
        AgentSessionKnowledgeBase.session_id == session.id
    ).limit(1)))
    try:
        validate_rag_binding(body, has_kb)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    session.tools = body.tools
    session.config_version += 1
    await db.execute(delete(AgentSubagentDefinition).where(AgentSubagentDefinition.session_id == session.id))
    db.add_all(AgentSubagentDefinition(
        session_id=session.id, name=item.name, description=item.description,
        system_prompt=item.system_prompt, tools=item.tools,
    ) for item in body.subagents)
    await db.commit()
    await db.refresh(session)
    knowledge_bases, latest_runs = await session_details(db, [session])
    return render_session(session, knowledge_bases[session.id], latest_runs.get(session.id))


@router.get("/{session_id}/agent-config")
async def get_agent_config(
    session_id: UUID,
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db),
):
    session = await owned(session_id, user, db)
    definitions = (await db.scalars(select(AgentSubagentDefinition).where(
        AgentSubagentDefinition.session_id == session.id
    ).order_by(AgentSubagentDefinition.name))).all()
    return {
        "tools": session.tools,
        "config_version": session.config_version,
        "subagents": [{"name": item.name, "description": item.description,
                       "system_prompt": item.system_prompt, "tools": item.tools} for item in definitions],
    }


@router.put("/{session_id}/model-config")
async def set_model_config(
    session_id: UUID,
    body: SessionModelConfigInput,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    session = await owned(session_id, user, db, lock=True)
    running = await family_running_run(db, session.id)
    if running is not None:
        raise HTTPException(409, "session_busy")
    await validate_model_config(db, user.id, session.workspace_id, body)
    session.provider_binding_id = body.provider_binding_id
    session.model_id = body.model_id
    session.thinking_level = body.thinking_level
    await db.commit()
    await db.refresh(session)
    knowledge_bases, latest_runs = await session_details(db, [session])
    return render_session(session, knowledge_bases[session.id], latest_runs.get(session.id))


@router.get("")
async def list_sessions(
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    rows = (
        await db.scalars(
            select(AgentSession)
            .where(AgentSession.user_id == user.id, AgentSession.parent_session_id.is_(None))
            .order_by(AgentSession.updated_at.desc(), AgentSession.id.desc())
        )
    ).all()
    knowledge_bases, latest_runs = await session_details(db, list(rows))
    return {
        "items": [
            render_session(item, knowledge_bases[item.id], latest_runs.get(item.id))
            for item in rows
        ]
    }


@router.get("/{session_id}")
async def get_session(
    session_id: UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    session = await owned(session_id, user, db, lock=True)
    knowledge_bases, latest_runs = await session_details(db, [session])
    return render_session(
        session, knowledge_bases[session.id], latest_runs.get(session.id)
    )


@router.patch("/{session_id}")
async def update_session(
    session_id: UUID,
    body: SessionTitleInput,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    title = body.title.strip()
    if not title:
        raise HTTPException(422, "invalid_session_title")
    session = await owned(session_id, user, db, lock=True)
    session.title = title
    await db.commit()
    await db.refresh(session)
    knowledge_bases, latest_runs = await session_details(db, [session])
    return render_session(
        session, knowledge_bases[session.id], latest_runs.get(session.id)
    )


@router.delete("/{session_id}", status_code=204)
async def delete_session(
    session_id: UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    session = await owned(session_id, user, db, lock=True)
    children = list((await db.scalars(select(AgentSession).where(
        AgentSession.parent_session_id == session.id,
        AgentSession.user_id == user.id,
    ).order_by(AgentSession.created_at, AgentSession.id))).all())
    running = await db.scalar(
        select(AgentRun.id)
        .where(AgentRun.session_id.in_([session.id, *(child.id for child in children)]), AgentRun.status == "running")
        .limit(1)
    )
    if running is not None:
        raise HTTPException(409, "session_busy")
    # A lazy Session that has never streamed has no Runtime data to clean.
    for owned_session in [*children, session]:
        if owned_session.pi_session_file_key:
            try:
                await RuntimeClient().delete_session(
                    str(user.id), str(owned_session.id), owned_session.pi_session_file_key
                )
            except RuntimeSessionBusyError as exc:
                raise HTTPException(409, "session_busy") from exc
            except RuntimeUnavailableError as exc:
                raise HTTPException(503, "session_delete_incomplete") from exc
    for child in children:
        await db.execute(delete(ChatMessage).where(ChatMessage.session_id == child.id))
        await db.execute(delete(AgentRun).where(AgentRun.session_id == child.id))
        await db.execute(delete(AgentSessionKnowledgeBase).where(AgentSessionKnowledgeBase.session_id == child.id))
        await db.delete(child)
    if children:
        await db.flush()
    await db.execute(delete(ChatMessage).where(ChatMessage.session_id == session.id))
    await db.execute(delete(AgentRun).where(AgentRun.session_id == session.id))
    await db.execute(
        delete(AgentSessionKnowledgeBase).where(
            AgentSessionKnowledgeBase.session_id == session.id
        )
    )
    await db.delete(session)
    await db.commit()
    return None


def render_child(session: AgentSession, run: AgentRun | None, knowledge_base_ids: list[str]) -> dict:
    return {
        **render_session(session, knowledge_base_ids, run),
        "read_only": True,
        "parent_session_id": str(session.parent_session_id),
        "parent_run_id": str(session.parent_run_id),
        "parent_tool_call_id": session.parent_tool_call_id,
        "task_index": session.task_index,
        "task": session.task_input,
        "subagent": session.subagent_snapshot,
    }


async def owned_child(parent_id: UUID, child_id: UUID, user: User, db: AsyncSession) -> AgentSession:
    await owned(parent_id, user, db)
    child = await db.scalar(select(AgentSession).where(
        AgentSession.id == child_id, AgentSession.parent_session_id == parent_id,
        AgentSession.user_id == user.id,
    ))
    if child is None:
        raise HTTPException(404, "session_not_found")
    return child


@router.get("/{session_id}/subagents")
async def list_subagent_sessions(
    session_id: UUID,
    limit: int = 50,
    offset: int = 0,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    await owned(session_id, user, db)
    if limit < 1 or limit > 100 or offset < 0:
        raise HTTPException(422, "invalid_pagination")
    rows = list((await db.scalars(select(AgentSession).where(
        AgentSession.parent_session_id == session_id,
        AgentSession.user_id == user.id,
    ).order_by(AgentSession.created_at.desc(), AgentSession.id.desc()).offset(offset).limit(limit))).all())
    knowledge_bases, runs = await session_details(db, rows)
    return {"items": [render_child(row, runs.get(row.id), knowledge_bases[row.id]) for row in rows]}


@router.get("/{session_id}/subagents/{child_id}")
async def get_subagent_session(
    session_id: UUID, child_id: UUID,
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db),
):
    child = await owned_child(session_id, child_id, user, db)
    knowledge_bases, runs = await session_details(db, [child])
    return render_child(child, runs.get(child.id), knowledge_bases[child.id])


@router.get("/{session_id}/subagents/{child_id}/messages")
async def get_subagent_messages(
    session_id: UUID, child_id: UUID,
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db),
):
    await owned_child(session_id, child_id, user, db)
    return await messages_for_session(child_id, db)


async def messages_for_session(session_id: UUID, db: AsyncSession) -> dict:
    rows = (await db.scalars(select(ChatMessage).where(
        ChatMessage.session_id == session_id
    ).order_by(ChatMessage.sequence))).all()
    return {"items": [dict(
        id=str(x.id), run_id=str(x.run_id) if x.run_id else None,
        role=x.role, content=x.content, sequence=x.sequence, status=x.status,
        created_at=x.created_at, tool_call_id=x.tool_call_id, tool_name=x.tool_name,
        arguments=x.arguments, result=x.result, is_error=x.is_error,
        payload_truncated=x.payload_truncated,
    ) for x in rows]}


@router.get("/{session_id}/messages")
async def messages(
    session_id: UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    await owned(session_id, user, db)
    return await messages_for_session(session_id, db)


@router.post("/{session_id}/messages:stream")
async def stream(
    session_id: UUID,
    body: MessageInput,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    session = await owned(session_id, user, db, lock=True)
    workspace = await db.scalar(
        select(Workspace)
        .where(Workspace.id == session.workspace_id, Workspace.user_id == user.id)
        .with_for_update()
    )
    if workspace is None or workspace.status != "active":
        raise HTTPException(409, "workspace_unavailable")
    if await family_running_run(db, session.id):
        raise HTTPException(409, "session_busy")
    await enforce_workspace_storage_limit(user.id)
    # Real ORM Sessions always expose these fields.  Keeping the legacy test
    # double path avoids coupling the run-uniqueness contract to Runtime I/O.
    runtime = await runtime_payload(db, session, user.id) if hasattr(session, "provider_binding_id") else None
    sequence = await db.scalar(
        select(func.coalesce(func.max(ChatMessage.sequence), 0)).where(
            ChatMessage.session_id == session_id
        )
    )
    run_values = {"session_id": session_id, "user_id": user.id}
    if runtime is not None:
        run_values.update({
            "provider_binding_id": UUID(runtime["provider_binding_id"]),
            "provider_id": runtime["provider_id"],
            "model_id": runtime["model_id"],
            "thinking_level": runtime["thinking_level"],
        })
    run = AgentRun(**run_values)
    try:
        # PostgreSQL checks the partial unique index during flush, not only at
        # commit.  Keep both operations in this boundary so a concurrent or
        # detached previous run is reported as an expected session conflict.
        db.add(run)
        await db.flush()
        db.add(
            ChatMessage(
                session_id=session_id,
                run_id=run.id,
                role="user",
                sequence=sequence + 1,
                content=body.content,
            )
        )
        if session.title is None:
            generated_title = " ".join(body.content.split())[:10]
            if generated_title:
                session.title = generated_title
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(409, "session_busy") from exc
    queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()
    _subscribers.setdefault(run.id, set()).add(queue)
    asyncio.create_task(consume_run(run.id, session_id, user.id, body.content))

    async def events():
        yield "event: message.accepted\ndata: {}\n\n"
        try:
            while True:
                name, data = await queue.get()
                yield f"event: {name}\ndata: {json.dumps(data)}\n\n"
                if name == "done":
                    return
        finally:
            _subscribers.get(run.id, set()).discard(queue)
            if not _subscribers.get(run.id):
                _subscribers.pop(run.id, None)

    return StreamingResponse(
        events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
    )


@router.post("/{session_id}/{action}", status_code=202)
async def control(
    session_id: UUID,
    action: str,
    body: MessageInput | None = None,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    if action not in {"abort", "steer", "follow-up"}:
        raise HTTPException(404, "not_found")
    await owned(session_id, user, db)
    await RuntimeClient().control(
        str(user.id), str(session_id), action, body.content if body else None
    )
    return {"status": "accepted"}
