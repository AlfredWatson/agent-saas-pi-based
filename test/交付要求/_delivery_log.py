#!/usr/bin/env python3
"""Human-readable step logging shared by the delivery acceptance scripts.

Each delivery ``*_flow.py`` script prints concise, human-readable progress for
every public-API step (login, upload, each processing stage, retrieval, graphs,
agent tool calls, history).  Structured diagnostics remain in each flow's
``events`` list and the JSON report; these helpers only improve terminal output.
"""

from __future__ import annotations

from typing import Any


def log(message: str) -> None:
    print(message)


def log_login(me: dict[str, Any]) -> None:
    log(f"[登录] user_id={me.get('id')} email={me.get('email')} status={me.get('status')}")


def log_capabilities(capabilities: dict[str, Any]) -> None:
    log(f"[能力] vector_backends={capabilities.get('vector_backends')}")
    log(f"[能力] document_extensions={capabilities.get('document_extensions')}")
    log(f"[能力] chunking_strategies={capabilities.get('chunking_strategies')}")


def log_workspace(workspace: dict[str, Any], *, label: str = "工作区") -> None:
    log(
        f"[{label}] id={workspace.get('id')} name={workspace.get('name')!r} "
        f"status={workspace.get('status')} is_current={workspace.get('is_current')}"
    )


def log_knowledge_base(kb: dict[str, Any]) -> None:
    log(
        f"[知识库] id={kb.get('id')} name={kb.get('name')!r} "
        f"vector_backend={kb.get('vector_backend')} status={kb.get('status')} "
        f"concurrency={kb.get('concurrency')}"
    )


def log_model_config(model: dict[str, Any], *, kind: str) -> None:
    if kind == "embedding":
        log(
            f"[embedding 模型] model_name={model.get('model_name')} "
            f"embedding_dimension={model.get('embedding_dimension')} "
            f"verified_at={model.get('verified_at')}"
        )
    elif kind == "llm":
        log(
            f"[LLM 模型] model_name={model.get('model_name')} "
            f"protocol={model.get('protocol')} kind={model.get('kind')}"
        )
    elif kind == "reranker":
        log(
            f"[重排模型] model_name={model.get('model_name')} "
            f"protocol={model.get('protocol')} kind={model.get('kind')}"
        )


def _stages(document: dict[str, Any]) -> str:
    stages = document.get("stages", {})
    return ", ".join(f"{name}={stage.get('status')}" for name, stage in stages.items())


def document_summary(document: dict[str, Any]) -> str:
    return (
        f"document_id={document.get('id')} "
        f"original_filename={document.get('original_filename')!r} "
        f"stored_filename={document.get('stored_filename')!r} "
        f"extension={document.get('extension')} "
        f"size_bytes={document.get('size_bytes')} "
        f"sha256={document.get('sha256')} "
        f"storage_backend={document.get('storage_backend')} "
        f"chunking_strategy={document.get('chunking_strategy')} "
        f"chunking_config={document.get('chunking_config')} "
        f"stages=[{_stages(document)}]"
    )


def stage_summary(document: dict[str, Any], stage: str) -> str:
    state = document.get("stages", {}).get(stage, {})
    return (
        f"status={state.get('status')} progress={state.get('progress')} "
        f"message={state.get('message')!r} error={state.get('error')!r}"
    )


def job_summary(job: dict[str, Any]) -> str:
    return (
        f"job_id={job.get('id')} kind={job.get('kind')} status={job.get('status')} "
        f"attempt={job.get('attempt')} progress={job.get('progress')} "
        f"message={job.get('message')!r} error={job.get('error')!r}"
    )


def log_document_uploaded(document: dict[str, Any]) -> None:
    log(f"[文档上传] 完成：{document_summary(document)}")


def log_stage_completed(
    flow: Any,
    kb_id: str,
    document_id: str,
    stage: str,
    job_ids: list[str],
    *,
    label: str,
) -> None:
    """Print the document stage state together with its processing job(s)."""
    document = flow.json_request(
        "GET",
        f"{flow.kb_root(kb_id)}/documents/{document_id}",
        label=f"log-{label}-document",
    )
    jobs = {
        job["id"]: job
        for job in flow.json_request(
            "GET", f"{flow.kb_root(kb_id)}/jobs", label=f"log-{label}-jobs"
        ).get("items", [])
    }
    log(f"[{label}] 完成：{stage_summary(document, stage)}")
    for job_id in job_ids:
        job = jobs.get(job_id)
        if job:
            log(f"  └─ 任务 {job_summary(job)}")


def log_retrieve(result: dict[str, Any], *, mode: str, limit: int = 5) -> None:
    items = result.get("items", [])
    log(f"[检索/{mode}] 命中 {len(items)} 条")
    for item in items[:limit]:
        score = item.get("score")
        retrieval_score = item.get("retrieval_score")
        extra = f" retrieval_score={retrieval_score}" if retrieval_score is not None else ""
        log(
            f"  └─ document_id={item.get('document_id')} "
            f"chunk_id={item.get('chunk_id')} score={score}{extra}"
        )
    if "rerank" in result:
        log(f"  └─ rerank={result.get('rerank')}")


def log_graphs(graphs: list[dict[str, Any]]) -> None:
    for graph in graphs:
        log(
            f"  └─ graph_id={graph.get('id')} name={graph.get('name')!r} "
            f"kind={graph.get('kind')} status={graph.get('status')} "
            f"source_document_id={graph.get('source_document_id')}"
        )


def log_graph_detail(detail: dict[str, Any]) -> None:
    log(f"  └─ 节点数={len(detail.get('nodes', []))} 边数={len(detail.get('edges', []))}")


def log_graph_retrieve(result: dict[str, Any]) -> None:
    log(
        f"[检索/graph] nodes={len(result.get('nodes', []))} "
        f"edges={len(result.get('edges', []))} evidence={len(result.get('evidence', []))}"
    )


def log_agent_events(events: list[tuple[str, dict[str, Any]]], text: str) -> None:
    names = [name for name, _ in events]
    log(f"[SSE] 事件序列：{names}")
    log(f"[SSE] assistant 文本增量合计 {len(text)} 字符")
    for name, payload in events:
        if name == "tool.started":
            log(
                f"  └─ tool.started  tool={payload.get('toolName')} "
                f"toolCallId={payload.get('toolCallId')} args={payload.get('args')}"
            )
        elif name == "tool.completed":
            log(
                f"  └─ tool.completed  tool={payload.get('toolName')} "
                f"toolCallId={payload.get('toolCallId')} isError={payload.get('isError')} "
                f"result={payload.get('result')}"
            )


def log_history(history: list[dict[str, Any]]) -> None:
    log(
        f"[历史] 消息条数={len(history)} "
        f"角色序列={[item.get('role') for item in history]}"
    )
    for item in history:
        role = item.get("role")
        if role == "tool_call":
            log(
                f"  └─ tool_call  tool={item.get('tool_name')} "
                f"toolCallId={item.get('tool_call_id')} arguments={item.get('arguments')}"
            )
        elif role == "tool_result":
            log(
                f"  └─ tool_result  tool={item.get('tool_name')} "
                f"toolCallId={item.get('tool_call_id')} is_error={item.get('is_error')} "
                f"result={item.get('result')}"
            )
