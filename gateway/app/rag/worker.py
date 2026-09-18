from __future__ import annotations

import asyncio
import logging
import math
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from langchain_core.documents import Document
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import get_settings
from ..core.encryption import encrypt
from ..db.session import SessionLocal
from .cache import RagCache
from .chunking import content_hash, split_documents, token_count
from .file_storage import get_file_storage
from .graph_store import get_graph_store
from .model_clients import chat_client, embedding_client, input_from_stored
from .models import (
    Chunk,
    ChunkVector,
    DocumentBlock,
    GraphArtifact,
    GraphEdge,
    GraphEvidence,
    GraphNode,
    KnowledgeBase,
    ProcessingJob,
    RagDocument,
    RagModelConfig,
    RagOperation,
)
from .processors import get_document_processor
from .schemas import GraphExtraction
from .startup import verify_rag_database
from .vector_store import PostgresVectorStore

logger = logging.getLogger("uvicorn.error")
STAGE_FIELD = {
    "parsing": "parsing",
    "chunking": "chunking",
    "vectorization": "vectorization",
    "graph_extraction": "graph",
}


class RagWorker:
    def __init__(self, worker_id: str | None = None):
        self.settings = get_settings()
        self.worker_id = (
            worker_id or f"{os.uname().nodename}-{os.getpid()}-{uuid4().hex[:8]}"
        )
        self.cache = RagCache()
        self.tasks: set[asyncio.Task] = set()
        self.maximum_tasks = (
            self.settings.rag_max_parsing_concurrency
            + self.settings.rag_max_chunking_concurrency
            + self.settings.rag_max_embedding_concurrency
            + self.settings.rag_max_graph_concurrency
        )

    async def close(self) -> None:
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.cache.close()

    async def run_forever(self) -> None:
        logger.info("RAG worker %s started", self.worker_id)
        await verify_rag_database()
        await self.cache.ping()
        while True:
            self.tasks = {task for task in self.tasks if not task.done()}
            claimed = False
            if len(self.tasks) < self.maximum_tasks:
                operation_id = await self.claim_operation()
                if operation_id and len(self.tasks) < self.maximum_tasks:
                    self._start(self.process_operation(operation_id))
                    claimed = True
                job_id = (
                    await self.claim_job()
                    if len(self.tasks) < self.maximum_tasks
                    else None
                )
                if job_id:
                    self._start(self.process_job(job_id))
                    claimed = True
            if not claimed:
                await asyncio.sleep(self.settings.rag_worker_poll_seconds)

    def _start(self, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def claim_job(self) -> UUID | None:
        now = datetime.now(UTC)
        async with SessionLocal() as db, db.begin():
            await db.execute(
                update(ProcessingJob)
                .where(
                    ProcessingJob.status == "running",
                    ProcessingJob.lease_expires_at < now,
                )
                .values(
                    status="queued",
                    leased_by=None,
                    lease_expires_at=None,
                    message="worker lease expired; queued for recovery",
                )
            )
            candidates = list(
                (
                    await db.scalars(
                        select(ProcessingJob)
                        .join(
                            KnowledgeBase,
                            KnowledgeBase.id == ProcessingJob.knowledge_base_id,
                        )
                        .where(
                            ProcessingJob.status == "queued",
                            KnowledgeBase.status == "active",
                        )
                        .order_by(ProcessingJob.created_at)
                        .with_for_update(skip_locked=True)
                        .limit(30)
                    )
                ).all()
            )
            for job in candidates:
                global_limit = {
                    "parsing": self.settings.rag_max_parsing_concurrency,
                    "chunking": self.settings.rag_max_chunking_concurrency,
                    "vectorization": self.settings.rag_max_embedding_concurrency,
                    "graph_extraction": self.settings.rag_max_graph_concurrency,
                }[job.kind]
                await db.execute(
                    select(
                        func.pg_advisory_xact_lock(
                            func.hashtextextended(f"rag-global:{job.kind}", 0)
                        )
                    )
                )
                global_running = await db.scalar(
                    select(func.count())
                    .select_from(ProcessingJob)
                    .where(
                        ProcessingJob.kind == job.kind,
                        ProcessingJob.status == "running",
                    )
                )
                if global_running >= global_limit:
                    continue
                kb = await db.get(KnowledgeBase, job.knowledge_base_id)
                limit = {
                    "parsing": kb.parsing_concurrency,
                    "chunking": kb.chunking_concurrency,
                    "vectorization": kb.embedding_concurrency,
                    "graph_extraction": kb.graph_concurrency,
                }[job.kind]
                await db.execute(
                    select(
                        func.pg_advisory_xact_lock(
                            func.hashtextextended(f"{kb.id}:{job.kind}", 0)
                        )
                    )
                )
                running = await db.scalar(
                    select(func.count())
                    .select_from(ProcessingJob)
                    .where(
                        ProcessingJob.knowledge_base_id == kb.id,
                        ProcessingJob.kind == job.kind,
                        ProcessingJob.status == "running",
                    )
                )
                if running >= limit:
                    continue
                job.status = "running"
                job.attempt += 1
                job.started_at = job.started_at or now
                job.heartbeat_at = now
                job.leased_by = self.worker_id
                job.lease_expires_at = now + timedelta(
                    seconds=self.settings.rag_job_lease_seconds
                )
                job.message = "running"
                document = await db.get(RagDocument, job.document_id)
                prefix = STAGE_FIELD[job.kind]
                setattr(document, f"{prefix}_status", "running")
                setattr(document, f"{prefix}_message", "running")
                setattr(document, f"{prefix}_error", None)
                setattr(document, f"{prefix}_updated_at", func.now())
                return job.id
        return None

    async def claim_operation(self) -> UUID | None:
        now = datetime.now(UTC)
        async with SessionLocal() as db, db.begin():
            await db.execute(
                update(RagOperation)
                .where(
                    RagOperation.status == "running",
                    RagOperation.lease_expires_at < now,
                )
                .values(
                    status="queued",
                    leased_by=None,
                    lease_expires_at=None,
                    message="worker lease expired; queued for recovery",
                )
            )
            operation = await db.scalar(
                select(RagOperation)
                .where(RagOperation.status == "queued")
                .order_by(RagOperation.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if operation is None:
                return None
            operation.status = "running"
            operation.attempt += 1
            operation.started_at = operation.started_at or now
            operation.message = "running"
            operation.leased_by = self.worker_id
            operation.heartbeat_at = now
            operation.lease_expires_at = now + timedelta(
                seconds=self.settings.rag_job_lease_seconds
            )
            return operation.id

    async def _progress(
        self,
        db: AsyncSession,
        job: ProcessingJob,
        current: int,
        total: int,
        message: str,
    ) -> None:
        await self._assert_job_active(db, job)
        now = datetime.now(UTC)
        job.progress_current = current
        job.progress_total = total
        job.message = message
        job.heartbeat_at = now
        job.lease_expires_at = now + timedelta(
            seconds=self.settings.rag_job_lease_seconds
        )
        document = await db.get(RagDocument, job.document_id)
        if document is None or document.status != "active":
            raise RuntimeError("document_unavailable")
        prefix = STAGE_FIELD[job.kind]
        setattr(
            document, f"{prefix}_progress", int(current * 100 / total) if total else 0
        )
        setattr(document, f"{prefix}_message", message)
        setattr(document, f"{prefix}_updated_at", now)
        await db.commit()

    async def _assert_job_active(
        self, db: AsyncSession, job: ProcessingJob, *, lock: bool = False
    ) -> None:
        statement = select(ProcessingJob).where(ProcessingJob.id == job.id)
        if lock:
            statement = statement.with_for_update()
        current = await db.scalar(statement.execution_options(populate_existing=True))
        if (
            current is None
            or current.status != "running"
            or current.leased_by != self.worker_id
        ):
            raise RuntimeError("job_cancelled")

    async def _heartbeat(self, job_id: UUID) -> None:
        interval = max(10, self.settings.rag_job_lease_seconds // 3)
        while True:
            await asyncio.sleep(interval)
            now = datetime.now(UTC)
            async with SessionLocal() as db:
                result = await db.execute(
                    update(ProcessingJob)
                    .where(
                        ProcessingJob.id == job_id,
                        ProcessingJob.status == "running",
                        ProcessingJob.leased_by == self.worker_id,
                    )
                    .values(
                        heartbeat_at=now,
                        lease_expires_at=now
                        + timedelta(seconds=self.settings.rag_job_lease_seconds),
                    )
                )
                await db.commit()
                if not result.rowcount:
                    return

    async def _operation_heartbeat(self, operation_id: UUID) -> None:
        interval = max(10, self.settings.rag_job_lease_seconds // 3)
        while True:
            await asyncio.sleep(interval)
            now = datetime.now(UTC)
            async with SessionLocal() as db:
                result = await db.execute(
                    update(RagOperation)
                    .where(
                        RagOperation.id == operation_id,
                        RagOperation.status == "running",
                        RagOperation.leased_by == self.worker_id,
                    )
                    .values(
                        heartbeat_at=now,
                        lease_expires_at=now
                        + timedelta(seconds=self.settings.rag_job_lease_seconds),
                    )
                )
                await db.commit()
                if not result.rowcount:
                    return

    async def process_job(self, job_id: UUID) -> None:
        heartbeat = asyncio.create_task(self._heartbeat(job_id))
        try:
            try:
                async with SessionLocal() as db:
                    job = await db.get(ProcessingJob, job_id)
                    if (
                        job is None
                        or job.status != "running"
                        or job.leased_by != self.worker_id
                    ):
                        return
                    if job.kind == "parsing":
                        await self.process_parsing(db, job)
                    elif job.kind == "chunking":
                        await self.process_chunking(db, job)
                    elif job.kind == "vectorization":
                        await self.process_vectorization(db, job)
                    else:
                        await self.process_graph(db, job)
                    job = await db.get(ProcessingJob, job_id)
                    if job is None:
                        return
                    job.status = "succeeded"
                    job.progress_current = job.progress_total
                    job.message = "completed"
                    job.error = None
                    job.finished_at = datetime.now(UTC)
                    job.leased_by = None
                    job.lease_expires_at = None
                    document = await db.get(RagDocument, job.document_id)
                    prefix = STAGE_FIELD[job.kind]
                    setattr(document, f"{prefix}_status", "succeeded")
                    setattr(document, f"{prefix}_progress", 100)
                    setattr(document, f"{prefix}_message", "completed")
                    setattr(document, f"{prefix}_error", None)
                    setattr(document, f"{prefix}_updated_at", datetime.now(UTC))
                    await db.commit()
            except Exception as exc:
                logger.exception("RAG job %s failed", job_id)
                async with SessionLocal() as db:
                    job = await db.get(ProcessingJob, job_id)
                    if job is None:
                        return
                    job.status = "failed"
                    job.error = str(exc)[:4000]
                    job.message = "failed"
                    job.finished_at = datetime.now(UTC)
                    job.leased_by = None
                    job.lease_expires_at = None
                    document = await db.get(RagDocument, job.document_id)
                    if document:
                        prefix = STAGE_FIELD[job.kind]
                        setattr(document, f"{prefix}_status", "failed")
                        setattr(document, f"{prefix}_message", str(exc)[:4000])
                        setattr(document, f"{prefix}_error", str(exc)[:4000])
                        setattr(document, f"{prefix}_updated_at", datetime.now(UTC))
                    await db.commit()
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _model_config(
        self, db: AsyncSession, kb_id: UUID, kind: str, fingerprint: str
    ) -> RagModelConfig:
        config = await db.scalar(
            select(RagModelConfig).where(
                RagModelConfig.knowledge_base_id == kb_id,
                RagModelConfig.kind == kind,
                RagModelConfig.fingerprint == fingerprint,
            )
        )
        if config is None:
            raise RuntimeError(f"{kind}_model_configuration_changed")
        return config

    async def process_parsing(self, db: AsyncSession, job: ProcessingJob) -> None:
        document = await db.get(RagDocument, job.document_id)
        if document is None:
            raise RuntimeError("document_unavailable")
        generation = int(job.config_snapshot["generation"])
        if document.processing_generation != generation:
            raise RuntimeError("document_generation_changed")
        if await db.scalar(
            select(DocumentBlock.id)
            .where(DocumentBlock.document_id == document.id)
            .limit(1)
        ):
            raise RuntimeError("document_blocks_already_exist")
        backend = str(job.config_snapshot["processor_backend"])
        content = await get_file_storage(document.storage_backend, db).read(document)
        parsed = await asyncio.to_thread(
            get_document_processor(backend).parse,
            document.stored_filename,
            content,
        )
        if not parsed:
            raise RuntimeError("document_contains_no_extractable_text")
        await self._assert_job_active(db, job, lock=True)
        # Publish all parsed blocks together.  A parser failure leaves no
        # user-visible partial result and parsing never uses Redis.
        for ordinal, item in enumerate(parsed):
            db.add(
                DocumentBlock(
                    document_id=document.id,
                    ordinal=ordinal,
                    text=item.page_content,
                    metadata_=item.metadata,
                    content_hash=content_hash(item.page_content),
                )
            )
        job.progress_current = job.progress_total = len(parsed)
        job.message = f"parsed {len(parsed)} blocks"

    async def process_chunking(self, db: AsyncSession, job: ProcessingJob) -> None:
        document = await db.get(RagDocument, job.document_id)
        kb = await db.get(KnowledgeBase, job.knowledge_base_id)
        snapshot = job.config_snapshot
        generation = int(snapshot["generation"])
        if document.processing_generation != generation:
            raise RuntimeError("document_generation_changed")
        blocks = list(
            (
                await db.scalars(
                    select(DocumentBlock)
                    .where(DocumentBlock.document_id == document.id)
                    .order_by(DocumentBlock.ordinal)
                )
            ).all()
        )
        if not blocks:
            raise RuntimeError("document_has_no_blocks")
        langchain_docs = [
            Document(
                page_content=block.text,
                metadata={**block.metadata_, "_block_id": str(block.id)},
            )
            for block in blocks
        ]
        embeddings = None
        if snapshot["strategy"] == "semantic":
            config = await self._model_config(
                db, kb.id, "embedding", str(snapshot["model_fingerprint"])
            )
            embeddings = embedding_client(input_from_stored(config))
        pieces = await asyncio.to_thread(
            split_documents,
            snapshot["strategy"],
            snapshot["config"],
            langchain_docs,
            embeddings,
        )
        if not pieces:
            raise RuntimeError("document_has_no_chunks")
        existing = await db.scalar(
            select(Chunk.id)
            .where(Chunk.document_id == document.id, Chunk.generation == generation)
            .limit(1)
        )
        if existing:
            raise RuntimeError("document_chunks_already_exist")
        block_map = {str(block.id): block for block in blocks}
        total = len(pieces)
        job.progress_total = total
        for ordinal, piece in enumerate(pieces):
            block = block_map[piece.metadata.pop("_block_id")]
            db.add(
                Chunk(
                    knowledge_base_id=kb.id,
                    document_id=document.id,
                    block_id=block.id,
                    generation=generation,
                    ordinal=ordinal,
                    text=piece.page_content,
                    token_count=token_count(piece.page_content),
                    content_hash=content_hash(piece.page_content),
                    metadata_=piece.metadata,
                    strategy_snapshot={
                        "strategy": snapshot["strategy"],
                        **snapshot["config"],
                    },
                )
            )
        await self._assert_job_active(db, job, lock=True)
        job.progress_current = total
        job.message = f"chunked {total}/{total}"

    async def process_vectorization(self, db: AsyncSession, job: ProcessingJob) -> None:
        document = await db.get(RagDocument, job.document_id)
        generation = int(job.config_snapshot["generation"])
        if document.processing_generation != generation:
            raise RuntimeError("document_generation_changed")
        config = await self._model_config(
            db,
            job.knowledge_base_id,
            "embedding",
            str(job.config_snapshot["model_fingerprint"]),
        )
        embeddings = embedding_client(input_from_stored(config))
        chunks = list(
            (
                await db.scalars(
                    select(Chunk)
                    .where(
                        Chunk.document_id == document.id, Chunk.generation == generation
                    )
                    .order_by(Chunk.ordinal)
                )
            ).all()
        )
        if not chunks:
            raise RuntimeError("document_has_no_chunks")
        vectors: list[tuple[Chunk, list[float]]] = []
        job.progress_total = len(chunks)
        for index, chunk in enumerate(chunks):
            await self._assert_job_active(db, job)
            key = self.cache.item_key(
                job.knowledge_base_id,
                document.id,
                "vector",
                config.fingerprint,
                generation,
                chunk.content_hash,
            )
            vector = await self.cache.get(key)
            if vector is None:
                vector = await embeddings.aembed_query(chunk.text)
                if len(vector) != config.embedding_dimension or any(
                    not math.isfinite(value) for value in vector
                ):
                    raise RuntimeError("invalid_embedding_response")
                await self._assert_job_active(db, job)
                await self.cache.put(job.knowledge_base_id, document.id, key, vector)
            if len(vector) != config.embedding_dimension or any(
                not math.isfinite(value) for value in vector
            ):
                raise RuntimeError("invalid_embedding_response")
            vectors.append((chunk, vector))
            await self._progress(
                db, job, index + 1, len(chunks), f"embedded {index + 1}/{len(chunks)}"
            )
        await self._assert_job_active(db, job, lock=True)
        store = PostgresVectorStore(db, job.knowledge_base_id, embeddings)
        await store.replace_document_vectors(document.id, config.fingerprint, vectors)

    async def process_graph(self, db: AsyncSession, job: ProcessingJob) -> None:
        document = await db.get(RagDocument, job.document_id)
        generation = int(job.config_snapshot["generation"])
        if document.processing_generation != generation:
            raise RuntimeError("document_generation_changed")
        config = await self._model_config(
            db,
            job.knowledge_base_id,
            "llm",
            str(job.config_snapshot["model_fingerprint"]),
        )
        extractor = chat_client(
            input_from_stored(config),
            max_tokens=self.settings.rag_graph_max_output_tokens,
        ).with_structured_output(GraphExtraction)
        chunks = list(
            (
                await db.scalars(
                    select(Chunk)
                    .where(
                        Chunk.document_id == document.id, Chunk.generation == generation
                    )
                    .order_by(Chunk.ordinal)
                )
            ).all()
        )
        if not chunks:
            raise RuntimeError("document_has_no_chunks")
        extracted: list[tuple[Chunk, GraphExtraction]] = []
        job.progress_total = len(chunks)
        for index, chunk in enumerate(chunks):
            await self._assert_job_active(db, job)
            key = self.cache.item_key(
                job.knowledge_base_id,
                document.id,
                "graph-v2",
                config.fingerprint,
                generation,
                chunk.content_hash,
            )
            cached = await self.cache.get(key)
            if cached is None:
                result = await extractor.ainvoke(
                    "Extract a compact factual knowledge graph from the text. "
                    "Return at most 8 nodes and 12 edges. Every edge endpoint must "
                    "also appear in nodes. Keep each description to one short sentence; "
                    "use at most four scalar properties per node or edge, and omit "
                    "properties that are not necessary.\n\n" + chunk.text
                )
                graph = (
                    result
                    if isinstance(result, GraphExtraction)
                    else GraphExtraction.model_validate(result)
                )
                cached = graph.model_dump()
                await self._assert_job_active(db, job)
                await self.cache.put(job.knowledge_base_id, document.id, key, cached)
            extracted.append((chunk, GraphExtraction.model_validate(cached)))
            await self._progress(
                db,
                job,
                index + 1,
                len(chunks),
                f"extracted graph {index + 1}/{len(chunks)}",
            )

        await self._assert_job_active(db, job, lock=True)
        kb = await db.get(KnowledgeBase, job.knowledge_base_id)
        store = get_graph_store(kb.graph_backend, db, kb.id)
        await store.replace_document_graph(document, config, extracted)

    async def process_operation(self, operation_id: UUID) -> None:
        heartbeat = asyncio.create_task(self._operation_heartbeat(operation_id))
        try:
            async with SessionLocal() as db:
                operation = await db.get(RagOperation, operation_id)
                if operation is None or operation.status != "running":
                    return
                if operation.kind == "copy":
                    await self.copy_knowledge_base(db, operation)
                else:
                    await self.delete_knowledge_base(db, operation)
                operation = await db.get(RagOperation, operation_id)
                if operation:
                    operation.status = "succeeded"
                    operation.message = "completed"
                    operation.error = None
                    operation.finished_at = datetime.now(UTC)
                    operation.leased_by = None
                    operation.lease_expires_at = None
                    await db.commit()
        except Exception as exc:
            logger.exception("RAG operation %s failed", operation_id)
            async with SessionLocal() as db:
                operation = await db.get(RagOperation, operation_id)
                if operation:
                    operation.error = str(exc)[:4000]
                    operation.leased_by = None
                    operation.lease_expires_at = None
                    if operation.attempt < self.settings.rag_operation_max_attempts:
                        operation.status = "queued"
                        operation.message = (
                            f"retrying after attempt {operation.attempt}: {exc}"
                        )[:4000]
                    else:
                        operation.status = "failed"
                        operation.message = "failed"
                        operation.finished_at = datetime.now(UTC)
                    if (
                        operation.status == "failed"
                        and operation.kind == "copy"
                        and operation.knowledge_base_id
                    ):
                        source = await db.get(
                            KnowledgeBase, operation.knowledge_base_id
                        )
                        target = await db.get(
                            KnowledgeBase,
                            UUID(operation.payload["target_knowledge_base_id"]),
                        )
                        if source:
                            source.status = "active"
                        if target:
                            target.status = "failed"
                    await db.commit()
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def copy_knowledge_base(
        self, db: AsyncSession, operation: RagOperation
    ) -> None:
        source = await db.get(KnowledgeBase, operation.knowledge_base_id)
        target = await db.get(
            KnowledgeBase, UUID(operation.payload["target_knowledge_base_id"])
        )
        if source is None or target is None:
            raise RuntimeError("copy_source_or_target_missing")
        for model in (
            await db.scalars(
                select(RagModelConfig).where(
                    RagModelConfig.knowledge_base_id == source.id
                )
            )
        ).all():
            secret = input_from_stored(model)
            copied = RagModelConfig(
                knowledge_base_id=target.id,
                kind=model.kind,
                protocol=model.protocol,
                base_url=model.base_url,
                model_name=model.model_name,
                thinking_effort=model.thinking_effort,
                ciphertext=b"",
                nonce=b"",
                fingerprint=model.fingerprint,
                embedding_dimension=model.embedding_dimension,
                verified_at=model.verified_at,
            )
            db.add(copied)
            await db.flush()
            aad = f"rag:{target.id}:{model.kind}:{copied.id}:{model.protocol}".encode()
            copied.ciphertext, copied.nonce = encrypt(secret.api_key, aad)
        document_map: dict[UUID, RagDocument] = {}
        for document in (
            await db.scalars(
                select(RagDocument).where(RagDocument.knowledge_base_id == source.id)
            )
        ).all():
            copied = RagDocument(
                knowledge_base_id=target.id,
                original_filename=document.original_filename,
                stored_filename=document.stored_filename,
                content_type=document.content_type,
                extension=document.extension,
                sha256=document.sha256,
                size_bytes=document.size_bytes,
                storage_backend=document.storage_backend,
                storage_key=uuid4().hex,
                status=document.status,
                processing_generation=document.processing_generation,
                parsing_backend=document.parsing_backend,
                parsing_status=document.parsing_status,
                parsing_progress=document.parsing_progress,
                parsing_message=document.parsing_message,
                parsing_error=document.parsing_error,
                parsing_updated_at=document.parsing_updated_at,
                chunking_strategy=document.chunking_strategy,
                chunking_config=document.chunking_config,
                chunking_status=document.chunking_status,
                chunking_progress=document.chunking_progress,
                chunking_message=document.chunking_message,
                chunking_error=document.chunking_error,
                chunking_updated_at=document.chunking_updated_at,
                vectorization_status=document.vectorization_status,
                vectorization_progress=document.vectorization_progress,
                vectorization_message=document.vectorization_message,
                vectorization_error=document.vectorization_error,
                vectorization_updated_at=document.vectorization_updated_at,
                graph_status=document.graph_status,
                graph_progress=document.graph_progress,
                graph_message=document.graph_message,
                graph_error=document.graph_error,
                graph_updated_at=document.graph_updated_at,
            )
            db.add(copied)
            await db.flush()
            await get_file_storage(document.storage_backend, db).copy(document, copied)
            document_map[document.id] = copied
        block_map: dict[UUID, DocumentBlock] = {}
        source_document_ids = set(document_map)
        if source_document_ids:
            for block in (
                await db.scalars(
                    select(DocumentBlock).where(
                        DocumentBlock.document_id.in_(source_document_ids)
                    )
                )
            ).all():
                copied = DocumentBlock(
                    document_id=document_map[block.document_id].id,
                    ordinal=block.ordinal,
                    text=block.text,
                    metadata_=block.metadata_,
                    content_hash=block.content_hash,
                )
                db.add(copied)
                await db.flush()
                block_map[block.id] = copied
        chunk_map: dict[UUID, Chunk] = {}
        for chunk in (
            await db.scalars(select(Chunk).where(Chunk.knowledge_base_id == source.id))
        ).all():
            copied = Chunk(
                knowledge_base_id=target.id,
                document_id=document_map[chunk.document_id].id,
                block_id=block_map[chunk.block_id].id,
                generation=chunk.generation,
                ordinal=chunk.ordinal,
                text=chunk.text,
                token_count=chunk.token_count,
                content_hash=chunk.content_hash,
                metadata_=chunk.metadata_,
                strategy_snapshot=chunk.strategy_snapshot,
            )
            db.add(copied)
            await db.flush()
            chunk_map[chunk.id] = copied
        for vector in (
            await db.scalars(
                select(ChunkVector).where(ChunkVector.knowledge_base_id == source.id)
            )
        ).all():
            db.add(
                ChunkVector(
                    knowledge_base_id=target.id,
                    document_id=document_map[vector.document_id].id,
                    chunk_id=chunk_map[vector.chunk_id].id,
                    model_fingerprint=vector.model_fingerprint,
                    dimension=vector.dimension,
                    embedding=vector.embedding,
                )
            )
        artifact_map: dict[UUID, GraphArtifact] = {}
        source_artifacts = list(
            (
                await db.scalars(
                    select(GraphArtifact).where(
                        GraphArtifact.knowledge_base_id == source.id
                    )
                )
            ).all()
        )
        for artifact in source_artifacts:
            copied = GraphArtifact(
                knowledge_base_id=target.id,
                source_document_id=document_map[artifact.source_document_id].id
                if artifact.source_document_id
                else None,
                kind=artifact.kind,
                name=artifact.name,
                model_fingerprint=artifact.model_fingerprint,
                source_graph_ids=[],
                status=artifact.status,
            )
            db.add(copied)
            await db.flush()
            artifact_map[artifact.id] = copied
        for artifact in source_artifacts:
            artifact_map[artifact.id].source_graph_ids = [
                str(artifact_map[UUID(item)].id) for item in artifact.source_graph_ids
            ]
        node_map: dict[UUID, GraphNode] = {}
        if artifact_map:
            for node in (
                await db.scalars(
                    select(GraphNode).where(GraphNode.artifact_id.in_(artifact_map))
                )
            ).all():
                copied = GraphNode(
                    artifact_id=artifact_map[node.artifact_id].id,
                    canonical_key=node.canonical_key,
                    name=node.name,
                    entity_type=node.entity_type,
                    description=node.description,
                    properties=node.properties,
                )
                db.add(copied)
                await db.flush()
                node_map[node.id] = copied
        edge_map: dict[UUID, GraphEdge] = {}
        if artifact_map:
            for edge in (
                await db.scalars(
                    select(GraphEdge).where(GraphEdge.artifact_id.in_(artifact_map))
                )
            ).all():
                copied = GraphEdge(
                    artifact_id=artifact_map[edge.artifact_id].id,
                    source_node_id=node_map[edge.source_node_id].id,
                    target_node_id=node_map[edge.target_node_id].id,
                    relation=edge.relation,
                    description=edge.description,
                    properties=edge.properties,
                )
                db.add(copied)
                await db.flush()
                edge_map[edge.id] = copied
            for evidence in (
                await db.scalars(
                    select(GraphEvidence).where(
                        GraphEvidence.artifact_id.in_(artifact_map)
                    )
                )
            ).all():
                db.add(
                    GraphEvidence(
                        artifact_id=artifact_map[evidence.artifact_id].id,
                        node_id=node_map[evidence.node_id].id
                        if evidence.node_id
                        else None,
                        edge_id=edge_map[evidence.edge_id].id
                        if evidence.edge_id
                        else None,
                        document_id=document_map[evidence.document_id].id,
                        chunk_id=chunk_map[evidence.chunk_id].id,
                        model_fingerprint=evidence.model_fingerprint,
                    )
                )
        for job in (
            await db.scalars(
                select(ProcessingJob).where(
                    ProcessingJob.knowledge_base_id == source.id
                )
            )
        ).all():
            db.add(
                ProcessingJob(
                    knowledge_base_id=target.id,
                    document_id=document_map[job.document_id].id,
                    kind=job.kind,
                    status=job.status,
                    config_snapshot=job.config_snapshot,
                    idempotency_key=uuid4().hex,
                    attempt=job.attempt,
                    progress_current=job.progress_current,
                    progress_total=job.progress_total,
                    message=job.message,
                    error=job.error,
                    started_at=job.started_at,
                    finished_at=job.finished_at,
                )
            )
        await db.flush()
        for source_id, copied in document_map.items():
            await self.cache.copy_document(source.id, source_id, target.id, copied.id)
        source.status = "active"
        target.status = "active"
        await db.commit()

    async def delete_knowledge_base(
        self, db: AsyncSession, operation: RagOperation
    ) -> None:
        kb = await db.get(KnowledgeBase, operation.knowledge_base_id)
        if kb is None:
            return
        await db.execute(
            update(ProcessingJob)
            .where(
                ProcessingJob.knowledge_base_id == kb.id,
                ProcessingJob.status == "running",
            )
            .values(
                status="failed", error="knowledge_base_deleted", finished_at=func.now()
            )
        )
        documents = (
            await db.scalars(
                select(RagDocument).where(RagDocument.knowledge_base_id == kb.id)
            )
        ).all()
        for document in documents:
            await self.cache.delete_document(kb.id, document.id)
            await get_file_storage(document.storage_backend, db).delete(document)
        await db.delete(kb)
        await db.commit()


async def run_worker() -> None:
    worker = RagWorker()
    try:
        await worker.run_forever()
    finally:
        await worker.close()
