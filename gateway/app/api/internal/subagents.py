"""Runtime-only durable subagent task orchestration."""

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...clients.agent_runtime import RuntimeClient
from ...db.models import (
    AgentRun, AgentSession, AgentSessionKnowledgeBase, AgentSubagentDefinition,
    ChatMessage, User,
)
from ...db.session import get_db
from ...services.agent_tools import effective_tools
from ..v1.sessions import consume_run
from .rag import authenticated_runtime_user

router = APIRouter(prefix="/internal/v1/runtime-subagents", tags=["internal"], include_in_schema=False)


class TaskInput(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    task: str = Field(min_length=1, max_length=100_000)


class BatchInput(BaseModel):
    parent_session_id: UUID
    tool_call_id: str = Field(min_length=1, max_length=256)
    tasks: list[TaskInput] = Field(min_length=1, max_length=16)


def task_result(child: AgentSession, run: AgentRun, output: str | None = None) -> dict:
    return {
        "session_id": str(child.id), "name": child.subagent_snapshot["name"],
        "status": run.status, "output": output,
        "error": run.error if run.status in {"failed", "cancelled", "interrupted"} else None,
    }


def bounded_output(value: str | None) -> str | None:
    if value is None:
        return None
    encoded = value.encode("utf-8")
    if len(encoded) <= 4096:
        return value
    return encoded[:4060].decode("utf-8", errors="ignore") + "\n[output truncated]"


async def child_and_run(db: AsyncSession, child_id: UUID, user_id: UUID, *, lock: bool = False):
    child = await db.scalar(select(AgentSession).where(
        AgentSession.id == child_id, AgentSession.user_id == user_id,
        AgentSession.parent_session_id.is_not(None),
    ))
    if child is None:
        raise HTTPException(404, "subagent_task_not_found")
    query = select(AgentRun).where(AgentRun.session_id == child.id)
    if lock:
        query = query.with_for_update()
    run = await db.scalar(query)
    if run is None:
        raise HTTPException(404, "subagent_task_not_found")
    return child, run


async def read_task_result(db: AsyncSession, child: AgentSession, run: AgentRun) -> dict:
    output = None
    if run.status not in {"queued", "running"}:
        boundary = await db.scalar(select(func.coalesce(func.max(ChatMessage.sequence), 0)).where(
            ChatMessage.session_id == child.id,
            ChatMessage.role.in_(["tool_call", "tool_result"]),
        ))
        texts = (await db.scalars(select(ChatMessage.content).where(
            ChatMessage.session_id == child.id,
            ChatMessage.role == "assistant",
            ChatMessage.sequence > boundary,
            ChatMessage.content != "[tool calls]",
        ).order_by(ChatMessage.sequence))).all()
        output = "\n".join(texts) if texts else None
    return task_result(child, run, bounded_output(output))


@router.post("/batches")
async def create_batch(
    body: BatchInput,
    user: User = Depends(authenticated_runtime_user),
    db: AsyncSession = Depends(get_db),
):
    parent = await db.scalar(select(AgentSession).where(
        AgentSession.id == body.parent_session_id, AgentSession.user_id == user.id,
        AgentSession.parent_session_id.is_(None),
    ).with_for_update())
    if parent is None:
        raise HTTPException(404, "session_not_found")
    run = await db.scalar(select(AgentRun).where(
        AgentRun.session_id == parent.id, AgentRun.status == "running"
    ).limit(1))
    if run is None:
        raise HTTPException(409, "session_not_running")
    existing = list((await db.scalars(select(AgentSession).where(
        AgentSession.parent_run_id == run.id,
        AgentSession.parent_tool_call_id == body.tool_call_id,
    ).order_by(AgentSession.task_index))).all())
    if existing:
        if len(existing) != len(body.tasks) or any(
            item.task_index != index or item.subagent_snapshot["name"] != task.name or item.task_input != task.task
            for index, (item, task) in enumerate(zip(existing, body.tasks, strict=True))
        ):
            raise HTTPException(409, "subagent_batch_conflict")
        return {"tasks": [{"session_id": str(item.id), "name": item.subagent_snapshot["name"]} for item in existing]}
    kb_ids = list((await db.scalars(select(AgentSessionKnowledgeBase.knowledge_base_id).where(
        AgentSessionKnowledgeBase.session_id == parent.id
    ))).all())
    if "call_subagents" not in effective_tools(parent.tools, bool(kb_ids)):
        raise HTTPException(409, "subagent_tool_disabled")
    definitions = {item.name: item for item in (await db.scalars(select(AgentSubagentDefinition).where(
        AgentSubagentDefinition.session_id == parent.id
    ))).all()}
    if any(task.name not in definitions for task in body.tasks):
        raise HTTPException(422, "unknown_subagent")
    created = []
    for index, task in enumerate(body.tasks):
        definition = definitions[task.name]
        snapshot = dict(name=definition.name, description=definition.description,
                        system_prompt=definition.system_prompt, tools=definition.tools)
        child = AgentSession(
            user_id=user.id, workspace_id=parent.workspace_id,
            provider_binding_id=run.provider_binding_id,
            model_id=run.model_id, thinking_level=run.thinking_level,
            parent_session_id=parent.id, parent_run_id=run.id,
            parent_tool_call_id=body.tool_call_id, task_index=index,
            task_input=task.task, subagent_snapshot=snapshot,
            tools=definition.tools, status="ready",
        )
        db.add(child)
        await db.flush()
        db.add(AgentRun(
            session_id=child.id, user_id=user.id, status="queued",
            provider_binding_id=run.provider_binding_id, provider_id=run.provider_id,
            model_id=run.model_id, thinking_level=run.thinking_level,
        ))
        db.add_all(AgentSessionKnowledgeBase(session_id=child.id, knowledge_base_id=kb_id) for kb_id in kb_ids)
        created.append(child)
    await db.commit()
    return {"tasks": [{"session_id": str(item.id), "name": item.subagent_snapshot["name"]} for item in created]}


@router.post("/tasks/{child_id}/run")
async def run_task(
    child_id: UUID,
    user: User = Depends(authenticated_runtime_user),
    db: AsyncSession = Depends(get_db),
):
    child, run = await child_and_run(db, child_id, user.id, lock=True)
    if run.status == "queued":
        parent_run = await db.get(AgentRun, child.parent_run_id)
        if parent_run is None or parent_run.status != "running":
            run.status, run.error, run.finished_at = "cancelled", "parent_run_ended", datetime.now(UTC)
        else:
            run.status = "running"
            db.add(ChatMessage(session_id=child.id, run_id=run.id, role="user", sequence=1, content=child.task_input))
            await db.commit()
            asyncio.create_task(consume_run(run.id, child.id, user.id, child.task_input))
            return task_result(child, run)
    await db.commit()
    return await read_task_result(db, child, run)


@router.get("/tasks/{child_id}")
async def get_task(
    child_id: UUID,
    user: User = Depends(authenticated_runtime_user),
    db: AsyncSession = Depends(get_db),
):
    child, run = await child_and_run(db, child_id, user.id)
    return await read_task_result(db, child, run)


@router.post("/tasks/{child_id}/cancel")
async def cancel_task(
    child_id: UUID,
    user: User = Depends(authenticated_runtime_user),
    db: AsyncSession = Depends(get_db),
):
    child, run = await child_and_run(db, child_id, user.id, lock=True)
    was_running = run.status == "running"
    if run.status in {"queued", "running"}:
        run.status, run.error, run.finished_at = "cancelled", "subagent_cancelled", datetime.now(UTC)
        await db.commit()
    if was_running:
        for delay in (0, 0.1, 0.5, 1):
            if delay:
                await asyncio.sleep(delay)
            try:
                await RuntimeClient().control(str(user.id), str(child.id), "abort")
                break
            except Exception:
                pass
    return {"status": run.status}
