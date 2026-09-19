#!/usr/bin/env python3
"""Black-box user-flow acceptance for the four-stage RAG API.

The script intentionally uses only public HTTP endpoints.  It does not import
Gateway modules and never reads PostgreSQL or Redis directly.

Run after Gateway, the RAG worker, Redis, PostgreSQL, and both local vLLM
servers are ready::

    uv run python test/rag_user_flow.py --report /tmp/rag-user-flow.json

By default all resources created by this run are removed.  Use
``--keep-resources`` to retain them for inspection.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import time
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from flow_env import configured_value, parse_dotenv


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FILES_DIR = ROOT / "test" / "files"
FIXTURE_NAMES = (
    "doc-test-1.docx",
    "doc-test-2.docx",
    "markdown-test-1.md",
    "markdown-test-2.md",
    "pdf-test-1.pdf",
    "pdf-test-2.pdf",
    "ppt-test-1.pptx",
    "table-test-1.xlsx",
    "table-test-3.xlsx",
)
IMAGE_ONLY_FIXTURE = "ppt-test-1.pptx"
MIME_TYPES = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".md": "text/markdown",
}
BACKENDS = {
    "file_backend": "postgresql",
    "block_backend": "postgresql",
    "chunk_backend": "postgresql",
    "vector_backend": "postgresql",
    "graph_backend": "postgresql",
}
SENSITIVE_KEYS = {
    "access_token",
    "api_key",
    "authorization",
    "ciphertext",
    "nonce",
    "password",
    "token",
}


class FlowError(RuntimeError):
    """An assertion or HTTP request in the acceptance flow failed."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise FlowError(message)


def redact(value: Any) -> Any:
    """Return JSON-safe diagnostic data without authentication material."""
    if isinstance(value, dict):
        return {
            key: "***" if key.casefold() in SENSITIVE_KEYS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    return value


def response_body(response: httpx.Response) -> Any:
    if not response.content:
        return None
    try:
        return redact(response.json())
    except ValueError:
        return response.text[:2_000]


def gateway_from_env_file() -> str:
    values = parse_dotenv(ROOT / ".env")
    host = values.get("GATEWAY_HOST", "127.0.0.1")
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    port = values.get("GATEWAY_PORT", "8000")
    return f"http://{host}:{port}/api/v1"


def normalized_api_base(url: str) -> str:
    return url.rstrip("/")


def model_ids(payload: Any) -> set[str]:
    if not isinstance(payload, dict):
        return set()
    data = payload.get("data", [])
    if not isinstance(data, list):
        return set()
    return {
        str(item["id"])
        for item in data
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def safe_stem(filename: str) -> str:
    stem = Path(filename.replace("\\", "/")).name
    stem = Path(stem).stem
    stem = re.sub(r"[^\w.-]+", "_", stem, flags=re.UNICODE)
    return re.sub(r"_+", "_", stem).strip("._") or "unnamed"


def stages_are(document: dict[str, Any], expected: str) -> bool:
    stages = document.get("stages", {})
    return all(stages.get(stage, {}).get("status") == expected for stage in stages)


@dataclass(frozen=True)
class Config:
    gateway: str
    email: str
    password: str
    files_dir: Path
    embedding_base_url: str
    embedding_model: str
    llm_base_url: str
    llm_model: str
    model_api_key: str
    request_timeout_seconds: float
    job_timeout_seconds: float
    poll_interval_seconds: float
    keep_resources: bool
    report: Path | None


class UserFlow:
    def __init__(self, config: Config):
        self.config = config
        self.client = httpx.Client(
            timeout=config.request_timeout_seconds,
            trust_env=False,
            follow_redirects=False,
        )
        self.token: str | None = None
        self.user_id: str | None = None
        self.capabilities: dict[str, Any] = {}
        self.workspace_id: str | None = None
        self.primary_kb_id: str | None = None
        self.isolation_kb_id: str | None = None
        self.copy_kb_id: str | None = None
        self.primary_documents: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.resources: dict[str, Any] = {}

    @property
    def headers(self) -> dict[str, str]:
        require(self.token is not None, "request attempted before login")
        return {"Authorization": f"Bearer {self.token}"}

    def close(self) -> None:
        self.client.close()

    def event(self, name: str, **details: Any) -> None:
        self.events.append(
            {
                "at": datetime.now(UTC).isoformat(),
                "name": name,
                "details": redact(details),
            }
        )

    def url(self, path: str) -> str:
        return f"{normalized_api_base(self.config.gateway)}{path}"

    def request(
        self,
        method: str,
        path: str,
        *,
        expected: int | tuple[int, ...] = 200,
        label: str,
        authenticated: bool = True,
        **kwargs: Any,
    ) -> httpx.Response:
        expected_statuses = (expected,) if isinstance(expected, int) else expected
        headers = dict(kwargs.pop("headers", {}))
        if authenticated:
            headers = {**self.headers, **headers}
        started = time.monotonic()
        try:
            response = self.client.request(
                method, self.url(path), headers=headers, **kwargs
            )
        except httpx.RequestError as exc:
            self.event(
                label,
                method=method,
                path=path,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise FlowError(f"{label}: cannot reach {self.url(path)}: {exc}") from exc
        elapsed_ms = round((time.monotonic() - started) * 1_000, 2)
        self.event(
            label,
            method=method,
            path=path,
            status_code=response.status_code,
            elapsed_ms=elapsed_ms,
            response=response_body(response),
        )
        if response.status_code not in expected_statuses:
            raise FlowError(
                f"{label}: expected HTTP {expected_statuses}, got {response.status_code}; "
                f"response={response_body(response)!r}"
            )
        return response

    def json_request(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        response = self.request(*args, **kwargs)
        try:
            payload = response.json()
        except ValueError as exc:
            raise FlowError(
                f"{kwargs.get('label', 'request')}: expected JSON response"
            ) from exc
        require(isinstance(payload, dict), "expected JSON object response")
        return payload

    def fixtures(self) -> list[Path]:
        paths = [self.config.files_dir / filename for filename in FIXTURE_NAMES]
        missing = [str(path) for path in paths if not path.is_file()]
        require(not missing, f"missing required RAG fixtures: {', '.join(missing)}")
        extensions = {path.suffix.lower() for path in paths}
        require(
            {".pdf", ".docx", ".xlsx", ".pptx", ".md"}.issubset(extensions),
            "fixtures do not cover PDF, DOCX, XLSX, PPTX, and Markdown",
        )
        return paths

    def preflight_model(self, name: str, base_url: str, model: str) -> None:
        endpoint = f"{normalized_api_base(base_url)}/models"
        started = time.monotonic()
        try:
            response = self.client.get(endpoint)
        except httpx.RequestError as exc:
            self.event(
                "preflight-model", model_kind=name, endpoint=endpoint, error=str(exc)
            )
            raise FlowError(f"{name} vLLM is unavailable at {endpoint}: {exc}") from exc
        self.event(
            "preflight-model",
            model_kind=name,
            endpoint=endpoint,
            status_code=response.status_code,
            elapsed_ms=round((time.monotonic() - started) * 1_000, 2),
            response=response_body(response),
        )
        if response.status_code != 200:
            raise FlowError(
                f"{name} vLLM preflight at {endpoint}: HTTP {response.status_code}; "
                f"response={response_body(response)!r}"
            )
        try:
            ids = model_ids(response.json())
        except ValueError as exc:
            raise FlowError(f"{name} vLLM returned invalid JSON at {endpoint}") from exc
        require(
            model in ids,
            f"{name} model {model!r} was not returned by {endpoint}; available={sorted(ids)}",
        )

    def preflight(self) -> list[Path]:
        fixtures = self.fixtures()
        self.preflight_model(
            "embedding", self.config.embedding_base_url, self.config.embedding_model
        )
        self.preflight_model("llm", self.config.llm_base_url, self.config.llm_model)
        return fixtures

    def login_and_validate_capabilities(self) -> None:
        login = self.json_request(
            "POST",
            "/auth/login",
            authenticated=False,
            label="login",
            json={"email": self.config.email, "password": self.config.password},
        )
        token = login.get("access_token")
        require(
            isinstance(token, str) and token,
            "login response did not include access_token",
        )
        self.token = token
        me = self.json_request("GET", "/auth/me", label="get-current-user")
        user_id = me.get("id")
        require(
            isinstance(user_id, str) and user_id,
            "current user response did not include id",
        )
        self.user_id = user_id

        capabilities = self.json_request(
            "GET", "/rag/capabilities", label="get-rag-capabilities"
        )
        for key in (
            "file_backends",
            "block_backends",
            "chunk_backends",
            "vector_backends",
            "graph_backends",
        ):
            require(
                capabilities.get(key) == ["postgresql"],
                f"unexpected {key}: {capabilities.get(key)!r}",
            )
        require(
            capabilities.get("document_processing_backends") == ["default"],
            "default document processor capability is missing",
        )
        strategies = capabilities.get("chunking_strategies")
        require(
            isinstance(strategies, dict)
            and set(strategies) == {"fixed", "regex", "semantic"},
            f"unexpected chunking strategies: {strategies!r}",
        )
        self.capabilities = capabilities

    def create_workspace_and_knowledge_bases(self) -> None:
        run_suffix = uuid4().hex[:10]
        workspace = self.json_request(
            "POST",
            "/workspaces",
            expected=201,
            label="create-workspace",
            json={"name": f"rag-e2e-{run_suffix}"},
        )
        workspace_id = workspace.get("id")
        require(isinstance(workspace_id, str), "workspace creation did not return id")
        self.workspace_id = workspace_id
        self.resources["workspace_id"] = workspace_id
        switched = self.json_request(
            "POST",
            f"/workspaces/{workspace_id}:switch",
            label="switch-workspace",
        )
        require(
            switched.get("id") == workspace_id and switched.get("is_current") is True,
            "workspace switch did not make the new workspace current",
        )

        for label in ("primary", "isolation"):
            body = {"name": f"rag-{label}-{run_suffix}", **BACKENDS}
            knowledge_base = self.json_request(
                "POST",
                f"/workspaces/{workspace_id}/knowledge-bases",
                expected=201,
                label=f"create-{label}-knowledge-base",
                json=body,
            )
            kb_id = knowledge_base.get("id")
            require(isinstance(kb_id, str), f"{label} knowledge base did not return id")
            require(
                all(
                    knowledge_base.get(key) == value for key, value in BACKENDS.items()
                ),
                f"{label} knowledge base did not retain PostgreSQL backends",
            )
            if label == "primary":
                self.primary_kb_id = kb_id
            else:
                self.isolation_kb_id = kb_id
            self.resources[f"{label}_knowledge_base_id"] = kb_id

        listed = self.json_request(
            "GET",
            f"/workspaces/{workspace_id}/knowledge-bases",
            label="list-created-knowledge-bases",
        ).get("items", [])
        ids = {item.get("id") for item in listed if isinstance(item, dict)}
        require(
            self.primary_kb_id in ids and self.isolation_kb_id in ids,
            "knowledge-base list does not contain both newly created knowledge bases",
        )
        created = [
            item
            for item in listed
            if isinstance(item, dict)
            and item.get("id") in {self.primary_kb_id, self.isolation_kb_id}
        ]
        require(len(created) == 2, "could not find exactly two new knowledge bases")
        require(
            all(
                {key: item.get(key) for key in BACKENDS} == BACKENDS for item in created
            ),
            "created knowledge bases have differing storage configurations",
        )

    def kb_root(self, kb_id: str) -> str:
        require(self.workspace_id is not None, "workspace was not created")
        return f"/workspaces/{self.workspace_id}/knowledge-bases/{kb_id}"

    def list_documents(self, kb_id: str, *, label: str) -> list[dict[str, Any]]:
        items = self.json_request(
            "GET", f"{self.kb_root(kb_id)}/documents", label=label
        ).get("items", [])
        require(
            isinstance(items, list), f"{label}: documents response has invalid items"
        )
        require(
            all(isinstance(item, dict) for item in items), f"{label}: invalid document"
        )
        return items

    def list_jobs(self, kb_id: str, *, label: str) -> list[dict[str, Any]]:
        items = self.json_request(
            "GET", f"{self.kb_root(kb_id)}/jobs", label=label
        ).get("items", [])
        require(isinstance(items, list), f"{label}: jobs response has invalid items")
        require(all(isinstance(item, dict) for item in items), f"{label}: invalid job")
        return items

    def upload_primary(self, fixtures: list[Path]) -> None:
        require(
            self.primary_kb_id is not None and self.user_id is not None,
            "setup incomplete",
        )
        with ExitStack() as stack:
            multipart = [
                (
                    "files",
                    (
                        path.name,
                        stack.enter_context(path.open("rb")),
                        MIME_TYPES[path.suffix.lower()],
                    ),
                )
                for path in fixtures
            ]
            payload = self.json_request(
                "POST",
                f"{self.kb_root(self.primary_kb_id)}/documents",
                label="upload-primary-fixtures",
                files=multipart,
            )
        require(
            payload.get("uploaded") == len(fixtures),
            f"unexpected upload result: {payload!r}",
        )
        require(
            payload.get("failed") == 0, f"fixture upload reported failures: {payload!r}"
        )
        items = payload.get("items", [])
        require(
            isinstance(items, list) and len(items) == len(fixtures),
            "upload item count mismatch",
        )
        documents = [
            item.get("document") for item in items if item.get("status") == "uploaded"
        ]
        require(
            all(isinstance(item, dict) for item in documents),
            "upload response lacks documents",
        )
        require(
            len(documents) == len(fixtures), "not every fixture produced a document"
        )
        ids = {item["id"] for item in documents}
        require(len(ids) == len(fixtures), "uploaded document ids are not unique")
        by_name = {item["original_filename"]: item for item in documents}
        require(
            set(by_name) == set(FIXTURE_NAMES),
            "uploaded file names differ from fixtures",
        )
        utc_day = datetime.now(UTC).strftime("%y%m%d")
        for path in fixtures:
            document = by_name[path.name]
            expected_name = (
                f"{self.user_id}_{utc_day}_{safe_stem(path.name)}{path.suffix.lower()}"
            )
            require(
                document.get("sha256") == hashlib.sha256(path.read_bytes()).hexdigest(),
                f"SHA-256 mismatch after upload for {path.name}",
            )
            require(
                document.get("storage_backend") == "postgresql",
                f"wrong storage backend for {path.name}",
            )
            require(
                document.get("stored_filename") == expected_name,
                f"stored filename mismatch for {path.name}: {document.get('stored_filename')!r}",
            )
            require(
                stages_are(document, "not_started"),
                f"upload unexpectedly started processing {path.name}",
            )
        self.primary_documents = by_name
        require(
            not self.list_jobs(
                self.primary_kb_id, label="verify-upload-created-no-jobs"
            ),
            "upload unexpectedly created processing jobs",
        )

    def verify_partial_upload(self) -> None:
        require(self.isolation_kb_id is not None, "isolation knowledge base missing")
        sample = self.config.files_dir / "markdown-test-2.md"
        with sample.open("rb") as handle:
            payload = self.json_request(
                "POST",
                f"{self.kb_root(self.isolation_kb_id)}/documents",
                label="verify-partial-upload",
                files=[
                    ("files", (sample.name, handle, "text/markdown")),
                    (
                        "files",
                        (
                            "unsupported.txt",
                            b"not a supported RAG document",
                            "text/plain",
                        ),
                    ),
                ],
            )
        require(
            payload.get("uploaded") == 1 and payload.get("failed") == 1,
            f"partial upload result was unexpected: {payload!r}",
        )
        items = payload.get("items", [])
        successful = [item for item in items if item.get("status") == "uploaded"]
        failed = [item for item in items if item.get("status") == "failed"]
        require(
            len(successful) == 1 and len(failed) == 1,
            "partial upload did not return per-file results",
        )
        require(
            failed[0].get("retryable") is False,
            "unsupported extension should not be retryable",
        )
        document = successful[0].get("document")
        require(
            isinstance(document, dict) and isinstance(document.get("id"), str),
            "partial upload did not return the successful document",
        )
        self.request(
            "DELETE",
            f"{self.kb_root(self.isolation_kb_id)}/documents/{document['id']}",
            expected=204,
            label="delete-partial-upload-document",
        )
        require(
            not self.list_documents(
                self.isolation_kb_id, label="verify-isolation-document-cleanup"
            ),
            "partial-upload test document was not deleted",
        )
        require(
            not self.list_jobs(
                self.isolation_kb_id, label="verify-isolation-upload-created-no-jobs"
            ),
            "isolation upload unexpectedly created processing jobs",
        )

    def processable_documents(self) -> dict[str, dict[str, Any]]:
        """Return fixtures with text under the default, intentionally no-OCR backend."""
        return {
            name: document
            for name, document in self.primary_documents.items()
            if name != IMAGE_ONLY_FIXTURE
        }

    def submit_parsing(self, document_ids: list[str], *, label: str) -> list[str]:
        require(self.primary_kb_id is not None, "primary knowledge base missing")
        payload = self.json_request(
            "POST",
            f"{self.kb_root(self.primary_kb_id)}/jobs/parsing",
            expected=202,
            label=label,
            json={
                "items": [
                    {"document_id": document_id, "processor_backend": "default"}
                    for document_id in document_ids
                ]
            },
        )
        return self.order_job_ids(
            document_ids, self.job_ids(payload, label, len(document_ids)), label
        )

    def submit_stage(
        self, kind: str, document_ids: list[str], *, label: str
    ) -> list[str]:
        require(self.primary_kb_id is not None, "primary knowledge base missing")
        payload = self.json_request(
            "POST",
            f"{self.kb_root(self.primary_kb_id)}/jobs/{kind}",
            expected=202,
            label=label,
            json={"document_ids": document_ids},
        )
        return self.order_job_ids(
            document_ids, self.job_ids(payload, label, len(document_ids)), label
        )

    @staticmethod
    def job_ids(payload: dict[str, Any], label: str, expected_count: int) -> list[str]:
        ids = payload.get("job_ids", [])
        require(
            isinstance(ids, list)
            and len(ids) == expected_count
            and all(isinstance(job_id, str) for job_id in ids),
            f"{label}: invalid job_ids response {payload!r}",
        )
        return ids

    def order_job_ids(
        self, document_ids: list[str], job_ids: list[str], label: str
    ) -> list[str]:
        """Align job IDs with documents instead of relying on SQL result order."""
        require(self.primary_kb_id is not None, "primary knowledge base missing")
        jobs = {
            job["id"]: job
            for job in self.list_jobs(self.primary_kb_id, label=f"{label}-resolve-jobs")
        }
        job_by_document: dict[str, str] = {}
        for job_id in job_ids:
            job = jobs.get(job_id)
            require(
                isinstance(job, dict), f"{label}: submitted job {job_id} was not listed"
            )
            document_id = job.get("document_id")
            require(
                isinstance(document_id, str) and document_id in document_ids,
                f"{label}: submitted job has an unexpected document: {job!r}",
            )
            require(
                document_id not in job_by_document,
                f"{label}: multiple submitted jobs target {document_id}",
            )
            job_by_document[document_id] = job_id
        require(
            set(job_by_document) == set(document_ids),
            f"{label}: missing document job mapping: {job_by_document!r}",
        )
        return [job_by_document[document_id] for document_id in document_ids]

    def wait_for_stage(
        self,
        document_ids: list[str],
        stage: str,
        job_ids: list[str],
        *,
        label: str,
    ) -> None:
        require(self.primary_kb_id is not None, "primary knowledge base missing")
        deadline = time.monotonic() + self.config.job_timeout_seconds
        job_ids_set = set(job_ids)
        last_state: dict[str, Any] = {}
        while time.monotonic() < deadline:
            documents = {
                document["id"]: document
                for document in self.list_documents(
                    self.primary_kb_id, label=f"{label}-documents"
                )
            }
            jobs = {
                job["id"]: job
                for job in self.list_jobs(self.primary_kb_id, label=f"{label}-jobs")
            }
            selected_documents = [
                documents.get(document_id) for document_id in document_ids
            ]
            selected_jobs = [jobs.get(job_id) for job_id in job_ids]
            last_state = {
                "documents": {
                    document_id: document.get("stages", {}).get(stage)
                    for document_id, document in zip(
                        document_ids, selected_documents, strict=True
                    )
                    if document is not None
                },
                "jobs": {
                    job_id: job
                    and {
                        key: job.get(key)
                        for key in ("status", "progress", "message", "error")
                    }
                    for job_id, job in zip(job_ids, selected_jobs, strict=True)
                },
            }
            require(
                all(selected_documents), f"{label}: a processed document disappeared"
            )
            require(all(selected_jobs), f"{label}: a submitted job disappeared")
            failed_documents = [
                document
                for document in selected_documents
                if document
                and document.get("stages", {}).get(stage, {}).get("status") == "failed"
            ]
            failed_jobs = [
                job for job in selected_jobs if job and job.get("status") == "failed"
            ]
            if failed_documents or failed_jobs:
                raise FlowError(
                    f"{label}: {stage} failed; state={redact(last_state)!r}"
                )
            if all(
                document
                and document.get("stages", {}).get(stage, {}).get("status")
                == "succeeded"
                and document.get("stages", {}).get(stage, {}).get("progress") == 100
                for document in selected_documents
            ) and all(
                job and job.get("status") == "succeeded" for job in selected_jobs
            ):
                for job in selected_jobs:
                    progress = job.get("progress", {}) if job else {}
                    require(
                        isinstance(progress, dict)
                        and progress.get("total", 0) > 0
                        and progress.get("current") == progress.get("total"),
                        f"{label}: succeeded job has incomplete progress: {job!r}",
                    )
                self.event(
                    f"{label}-completed",
                    stage=stage,
                    document_ids=document_ids,
                    job_ids=list(job_ids_set),
                    state=last_state,
                )
                return
            time.sleep(self.config.poll_interval_seconds)
        raise FlowError(
            f"{label}: timed out after {self.config.job_timeout_seconds}s; "
            f"last_state={redact(last_state)!r}"
        )

    def wait_for_expected_failure(
        self,
        document_id: str,
        stage: str,
        job_id: str,
        expected_error: str,
        *,
        label: str,
    ) -> None:
        """Verify a known image-only input fails without publishing blocks."""
        require(self.primary_kb_id is not None, "primary knowledge base missing")
        deadline = time.monotonic() + self.config.job_timeout_seconds
        last_state: dict[str, Any] = {}
        while time.monotonic() < deadline:
            document = self.document_detail(document_id, label=f"{label}-document")
            jobs = {
                job["id"]: job
                for job in self.list_jobs(self.primary_kb_id, label=f"{label}-jobs")
            }
            job = jobs.get(job_id)
            require(isinstance(job, dict), f"{label}: expected-failure job disappeared")
            document_stage = document.get("stages", {}).get(stage, {})
            last_state = {"document": document_stage, "job": job}
            if (
                document_stage.get("status") == "succeeded"
                or job.get("status") == "succeeded"
            ):
                raise FlowError(
                    f"{label}: image-only document unexpectedly succeeded: {last_state!r}"
                )
            if (
                document_stage.get("status") == "failed"
                and job.get("status") == "failed"
            ):
                error_text = " ".join(
                    str(value)
                    for value in (
                        document_stage.get("error"),
                        document_stage.get("message"),
                        job.get("error"),
                        job.get("message"),
                    )
                    if value
                )
                require(
                    expected_error in error_text,
                    f"{label}: unexpected parsing failure: {last_state!r}",
                )
                self.event(
                    f"{label}-completed",
                    stage=stage,
                    document_id=document_id,
                    job_id=job_id,
                    expected_error=expected_error,
                    state=last_state,
                )
                return
            time.sleep(self.config.poll_interval_seconds)
        raise FlowError(
            f"{label}: timed out waiting for expected failure; "
            f"last_state={redact(last_state)!r}"
        )

    def parse_and_configure_models(self) -> None:
        document_ids = [document["id"] for document in self.primary_documents.values()]
        jobs = self.submit_parsing(document_ids, label="submit-primary-parsing")
        job_by_document = dict(zip(document_ids, jobs, strict=True))
        image_only_id = self.primary_documents[IMAGE_ONLY_FIXTURE]["id"]
        self.wait_for_expected_failure(
            image_only_id,
            "parsing",
            job_by_document[image_only_id],
            "document_contains_no_extractable_text",
            label="wait-image-only-ppt-parsing-failure",
        )
        successful_document_ids = [
            document["id"] for document in self.processable_documents().values()
        ]
        self.wait_for_stage(
            successful_document_ids,
            "parsing",
            [job_by_document[document_id] for document_id in successful_document_ids],
            label="wait-text-documents-parsing",
        )
        require(self.primary_kb_id is not None, "primary knowledge base missing")
        self.request(
            "POST",
            f"{self.kb_root(self.primary_kb_id)}/jobs/parsing",
            expected=409,
            label="reject-duplicate-parsing",
            json={
                "items": [
                    {
                        "document_id": successful_document_ids[0],
                        "processor_backend": "default",
                    }
                ]
            },
        )

        embedding = self.json_request(
            "PUT",
            f"{self.kb_root(self.primary_kb_id)}/embedding-model",
            label="configure-embedding-model",
            json={
                "protocol": "openai",
                "base_url": normalized_api_base(self.config.embedding_base_url),
                "api_key": self.config.model_api_key,
                "model_name": self.config.embedding_model,
            },
        )
        require(
            embedding.get("embedding_dimension") == 4096,
            f"embedding model dimension is not 4096: {embedding!r}",
        )
        try:
            llm = self.json_request(
                "PUT",
                f"{self.kb_root(self.primary_kb_id)}/llm-model",
                label="configure-llm-model",
                json={
                    "protocol": "openai",
                    "base_url": normalized_api_base(self.config.llm_base_url),
                    "api_key": self.config.model_api_key,
                    "model_name": self.config.llm_model,
                },
            )
        except FlowError as exc:
            raise FlowError(
                f"{exc}\nLLM configuration verifies structured graph output. "
                "Check that the vLLM server supports the LangChain structured-output request."
            ) from exc
        require(
            llm.get("kind") == "llm", f"invalid LLM configuration response: {llm!r}"
        )
        models = self.json_request(
            "GET",
            f"{self.kb_root(self.primary_kb_id)}/models",
            label="list-configured-models",
        )
        rendered = json.dumps(models, ensure_ascii=False)
        require(
            "api_key" not in rendered and self.config.model_api_key not in rendered,
            "models API exposed an API key",
        )
        kinds = {
            item.get("kind")
            for item in models.get("items", [])
            if isinstance(item, dict)
        }
        require(
            kinds == {"embedding", "llm"}, f"unexpected configured model kinds: {kinds}"
        )

    def chunk_and_vectorize(self) -> None:
        require(self.primary_kb_id is not None, "primary knowledge base missing")
        regex_document = self.primary_documents["markdown-test-1.md"]
        semantic_document = self.primary_documents["markdown-test-2.md"]
        strategies = self.capabilities["chunking_strategies"]
        for document, strategy in (
            (regex_document, "regex"),
            (semantic_document, "semantic"),
        ):
            updated = self.json_request(
                "PUT",
                f"{self.kb_root(self.primary_kb_id)}/documents/{document['id']}/chunking-config",
                label=f"configure-{strategy}-chunking",
                json={"strategy": strategy, "config": strategies[strategy]},
            )
            require(
                updated.get("chunking_strategy") == strategy,
                f"{strategy} chunking configuration was not stored",
            )
        document_ids = [
            document["id"] for document in self.processable_documents().values()
        ]
        chunk_jobs = self.submit_stage(
            "chunking", document_ids, label="submit-primary-chunking"
        )
        self.wait_for_stage(
            document_ids, "chunking", chunk_jobs, label="wait-primary-chunking"
        )
        self.request(
            "POST",
            f"{self.kb_root(self.primary_kb_id)}/jobs/chunking",
            expected=409,
            label="reject-duplicate-chunking",
            json={"document_ids": [document_ids[0]]},
        )

        vector_jobs = self.submit_stage(
            "vectorization", document_ids, label="submit-primary-vectorization"
        )
        self.wait_for_stage(
            document_ids,
            "vectorization",
            vector_jobs,
            label="wait-primary-vectorization",
        )
        self.request(
            "POST",
            f"{self.kb_root(self.primary_kb_id)}/jobs/vectorization",
            expected=409,
            label="reject-duplicate-vectorization",
            json={"document_ids": [document_ids[0]]},
        )
        self.request(
            "PUT",
            f"{self.kb_root(self.primary_kb_id)}/embedding-model",
            expected=409,
            label="reject-locked-embedding-model",
            json={
                "protocol": "openai",
                "base_url": normalized_api_base(self.config.embedding_base_url),
                "api_key": f"{self.config.model_api_key}-lock-check",
                "model_name": self.config.embedding_model,
            },
        )

    def graph_retrieval_and_merge(self) -> None:
        require(self.primary_kb_id is not None, "primary knowledge base missing")
        graph_documents = [
            self.primary_documents["markdown-test-1.md"],
            self.primary_documents["markdown-test-2.md"],
        ]
        graph_document_ids = [document["id"] for document in graph_documents]
        graph_jobs = self.submit_stage(
            "graph-extraction",
            graph_document_ids,
            label="submit-markdown-graph-extraction",
        )
        self.wait_for_stage(
            graph_document_ids,
            "graph",
            graph_jobs,
            label="wait-markdown-graph-extraction",
        )
        self.request(
            "POST",
            f"{self.kb_root(self.primary_kb_id)}/jobs/graph-extraction",
            expected=409,
            label="reject-duplicate-graph-extraction",
            json={"document_ids": [graph_document_ids[0]]},
        )

        known_document_ids = {
            document["id"] for document in self.primary_documents.values()
        }
        for mode in ("vector", "hybrid"):
            result = self.json_request(
                "POST",
                f"{self.kb_root(self.primary_kb_id)}/retrieve",
                label=f"retrieve-{mode}",
                json={
                    "query": "Unity Codely Agent 前期推进计划",
                    "mode": mode,
                    "top_k": 5,
                },
            )
            items = result.get("items", [])
            require(
                result.get("mode") == mode and isinstance(items, list) and items,
                f"{mode} retrieval returned no results",
            )
            for item in items:
                require(
                    item.get("document_id") in known_document_ids,
                    f"{mode} retrieval leaked a document from another knowledge base",
                )
                score = item.get("score")
                require(
                    isinstance(score, (int, float)) and math.isfinite(score),
                    f"{mode} retrieval has invalid score: {item!r}",
                )

        filtered = self.json_request(
            "POST",
            f"{self.kb_root(self.primary_kb_id)}/retrieve",
            label="retrieve-vector-with-document-filter",
            json={
                "query": "Unity Codely Agent 前期推进计划",
                "mode": "vector",
                "document_ids": [graph_document_ids[0]],
                "top_k": 5,
            },
        )
        require(filtered.get("items"), "filtered vector retrieval returned no results")
        require(
            all(
                item.get("document_id") == graph_document_ids[0]
                for item in filtered["items"]
            ),
            "document-filtered retrieval returned another document",
        )
        graph_result = self.json_request(
            "POST",
            f"{self.kb_root(self.primary_kb_id)}/retrieve",
            label="retrieve-graph",
            json={"query": "Unity", "mode": "graph", "top_k": 5},
        )
        require(
            graph_result.get("nodes") and graph_result.get("evidence"),
            f"graph retrieval returned no nodes or evidence: {graph_result!r}",
        )
        require(
            all(
                item.get("document_id") in set(graph_document_ids)
                for item in graph_result["evidence"]
            ),
            "graph retrieval evidence escaped selected graph documents",
        )

        graphs = self.list_graphs(self.primary_kb_id, label="list-document-graphs")
        document_graphs = [
            graph
            for graph in graphs
            if graph.get("kind") == "document"
            and graph.get("source_document_id") in set(graph_document_ids)
            and graph.get("status") == "ready"
        ]
        require(
            len(document_graphs) == 2,
            f"expected two ready document graphs, got {document_graphs!r}",
        )
        graph_ids = [graph["id"] for graph in document_graphs]
        merged = self.json_request(
            "POST",
            f"{self.kb_root(self.primary_kb_id)}/graphs:merge",
            expected=201,
            label="merge-markdown-graphs",
            json={"name": f"rag-e2e-merge-{uuid4().hex[:8]}", "graph_ids": graph_ids},
        )
        merged_id = merged.get("id")
        require(
            isinstance(merged_id, str) and merged.get("kind") == "merged",
            "graph merge failed",
        )
        graphs_after_merge = self.list_graphs(
            self.primary_kb_id, label="verify-merged-graph"
        )
        graph_ids_after_merge = {graph.get("id") for graph in graphs_after_merge}
        require(
            merged_id in graph_ids_after_merge
            and set(graph_ids).issubset(graph_ids_after_merge),
            "graph merge removed source graphs or did not persist the merged graph",
        )
        self.resources["merged_graph_id"] = merged_id

    def list_graphs(self, kb_id: str, *, label: str) -> list[dict[str, Any]]:
        items = self.json_request(
            "GET", f"{self.kb_root(kb_id)}/graphs", label=label
        ).get("items", [])
        require(
            isinstance(items, list) and all(isinstance(item, dict) for item in items),
            f"{label}: invalid graph list",
        )
        return items

    def wait_operation(self, operation_id: str, *, label: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.config.job_timeout_seconds
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = self.json_request(
                "GET", f"/rag/operations/{operation_id}", label=f"{label}-poll"
            )
            status = last.get("status")
            if status == "succeeded":
                return last
            if status == "failed":
                raise FlowError(f"{label}: maintenance operation failed: {last!r}")
            time.sleep(self.config.poll_interval_seconds)
        raise FlowError(f"{label}: maintenance operation timed out; last={last!r}")

    @staticmethod
    def document_signature(document: dict[str, Any]) -> tuple[Any, ...]:
        stages = document.get("stages", {})
        return (
            document.get("original_filename"),
            document.get("sha256"),
            document.get("size_bytes"),
            document.get("chunking_strategy"),
            tuple(
                sorted((name, stage.get("status")) for name, stage in stages.items())
            ),
        )

    @staticmethod
    def job_signature(job: dict[str, Any]) -> tuple[Any, ...]:
        return job.get("kind"), job.get("status"), job.get("attempt")

    def copy_and_compare(self) -> None:
        require(self.primary_kb_id is not None, "primary knowledge base missing")
        copied = self.json_request(
            "POST",
            f"{self.kb_root(self.primary_kb_id)}/copy",
            expected=202,
            label="copy-primary-knowledge-base",
            json={"name": f"rag-copy-{uuid4().hex[:10]}"},
        )
        operation_id = copied.get("operation_id")
        copy_kb_id = copied.get("target_knowledge_base_id")
        require(
            isinstance(operation_id, str) and isinstance(copy_kb_id, str),
            "copy response missing ids",
        )
        self.copy_kb_id = copy_kb_id
        self.resources["copy_knowledge_base_id"] = copy_kb_id
        self.wait_operation(operation_id, label="copy-knowledge-base")

        source = self.json_request(
            "GET", self.kb_root(self.primary_kb_id), label="get-primary-after-copy"
        )
        target = self.json_request(
            "GET", self.kb_root(copy_kb_id), label="get-copy-after-copy"
        )
        require(
            source.get("status") == target.get("status") == "active",
            "source or copy is not active",
        )
        require(
            all(
                source.get(key) == target.get(key) == value
                for key, value in BACKENDS.items()
            ),
            "copy changed storage backend configuration",
        )
        require(
            source.get("concurrency") == target.get("concurrency"),
            "copy changed concurrency configuration",
        )
        source_models = self.json_request(
            "GET",
            f"{self.kb_root(self.primary_kb_id)}/models",
            label="list-source-models-after-copy",
        ).get("items", [])
        target_models = self.json_request(
            "GET",
            f"{self.kb_root(copy_kb_id)}/models",
            label="list-copy-models-after-copy",
        ).get("items", [])
        model_keys = (
            "kind",
            "protocol",
            "base_url",
            "model_name",
            "embedding_dimension",
            "fingerprint",
        )
        require(
            sorted(tuple(item.get(key) for key in model_keys) for item in source_models)
            == sorted(
                tuple(item.get(key) for key in model_keys) for item in target_models
            ),
            "copy model configuration differs from source",
        )
        source_docs = self.list_documents(
            self.primary_kb_id, label="list-source-documents-after-copy"
        )
        copy_docs = self.list_documents(
            copy_kb_id, label="list-copy-documents-after-copy"
        )
        require(
            sorted(map(self.document_signature, source_docs))
            == sorted(map(self.document_signature, copy_docs)),
            "copy document metadata or stage state differs from source",
        )
        source_jobs = self.list_jobs(
            self.primary_kb_id, label="list-source-jobs-after-copy"
        )
        copy_jobs = self.list_jobs(copy_kb_id, label="list-copy-jobs-after-copy")
        require(
            sorted(map(self.job_signature, source_jobs))
            == sorted(map(self.job_signature, copy_jobs)),
            "copy job history differs from source",
        )
        source_graphs = self.list_graphs(
            self.primary_kb_id, label="list-source-graphs-after-copy"
        )
        copy_graphs = self.list_graphs(copy_kb_id, label="list-copy-graphs-after-copy")
        require(
            sorted(graph.get("kind") for graph in source_graphs)
            == sorted(graph.get("kind") for graph in copy_graphs),
            "copy graph artifacts differ from source",
        )
        for mode, query in (
            ("vector", "Unity Codely Agent"),
            ("hybrid", "Unity Codely Agent"),
            ("graph", "Unity"),
        ):
            result = self.json_request(
                "POST",
                f"{self.kb_root(copy_kb_id)}/retrieve",
                label=f"retrieve-{mode}-from-copy",
                json={"query": query, "mode": mode, "top_k": 5},
            )
            if mode == "graph":
                require(
                    result.get("nodes") and result.get("evidence"),
                    "copied graph is not retrievable",
                )
            else:
                require(
                    result.get("items"), f"copied {mode} vectors are not retrievable"
                )
        require(copy_docs, "copy has no documents for cross-knowledge-base validation")
        self.request(
            "POST",
            f"{self.kb_root(self.primary_kb_id)}/retrieve",
            expected=422,
            label="reject-cross-knowledge-base-document-filter",
            json={
                "query": "Unity",
                "mode": "vector",
                "document_ids": [copy_docs[0]["id"]],
            },
        )

    def document_detail(self, document_id: str, *, label: str) -> dict[str, Any]:
        require(self.primary_kb_id is not None, "primary knowledge base missing")
        return self.json_request(
            "GET",
            f"{self.kb_root(self.primary_kb_id)}/documents/{document_id}",
            label=label,
        )

    def delete_and_reprocess(self) -> None:
        require(self.primary_kb_id is not None, "primary knowledge base missing")
        target = self.primary_documents["markdown-test-2.md"]
        target_id = target["id"]
        merged_graph_id = self.resources.get("merged_graph_id")

        self.request(
            "DELETE",
            f"{self.kb_root(self.primary_kb_id)}/documents/{target_id}/vectors",
            expected=204,
            label="delete-target-vectors",
        )
        require(
            self.document_detail(target_id, label="verify-vectors-deleted")["stages"][
                "vectorization"
            ]["status"]
            == "not_started",
            "deleting vectors did not reset vectorization state",
        )
        vector_jobs = self.submit_stage(
            "vectorization", [target_id], label="resubmit-target-vectorization"
        )
        self.wait_for_stage(
            [target_id],
            "vectorization",
            vector_jobs,
            label="wait-target-revectorization",
        )

        self.request(
            "DELETE",
            f"{self.kb_root(self.primary_kb_id)}/documents/{target_id}/graph",
            expected=204,
            label="delete-target-graph",
        )
        detail = self.document_detail(target_id, label="verify-graph-deleted")
        require(
            detail["stages"]["graph"]["status"] == "not_started",
            "deleting graph did not reset state",
        )
        if isinstance(merged_graph_id, str):
            require(
                merged_graph_id
                not in {
                    graph.get("id")
                    for graph in self.list_graphs(
                        self.primary_kb_id, label="verify-dependent-merge-deleted"
                    )
                },
                "deleting source graph did not delete dependent merged graph",
            )
        graph_jobs = self.submit_stage(
            "graph-extraction", [target_id], label="resubmit-target-graph"
        )
        self.wait_for_stage(
            [target_id], "graph", graph_jobs, label="wait-target-regraph"
        )

        self.request(
            "DELETE",
            f"{self.kb_root(self.primary_kb_id)}/documents/{target_id}/chunks",
            expected=204,
            label="delete-target-chunks",
        )
        detail = self.document_detail(target_id, label="verify-chunks-deleted")
        require(
            detail["stages"]["chunking"]["status"] == "not_started"
            and detail["stages"]["vectorization"]["status"] == "not_started"
            and detail["stages"]["graph"]["status"] == "not_started",
            "deleting chunks did not reset downstream stages",
        )
        chunk_jobs = self.submit_stage(
            "chunking", [target_id], label="resubmit-target-chunking"
        )
        self.wait_for_stage(
            [target_id], "chunking", chunk_jobs, label="wait-target-rechunking"
        )
        vector_jobs = self.submit_stage(
            "vectorization", [target_id], label="resubmit-target-vectors-after-chunks"
        )
        self.wait_for_stage(
            [target_id],
            "vectorization",
            vector_jobs,
            label="wait-target-vectors-after-chunks",
        )
        graph_jobs = self.submit_stage(
            "graph-extraction", [target_id], label="resubmit-target-graph-after-chunks"
        )
        self.wait_for_stage(
            [target_id], "graph", graph_jobs, label="wait-target-graph-after-chunks"
        )

        self.request(
            "DELETE",
            f"{self.kb_root(self.primary_kb_id)}/documents/{target_id}/blocks",
            expected=204,
            label="delete-target-blocks",
        )
        detail = self.document_detail(target_id, label="verify-blocks-deleted")
        require(
            all(
                detail["stages"][stage]["status"] == "not_started"
                for stage in ("parsing", "chunking", "vectorization", "graph")
            ),
            "deleting blocks did not reset four stages",
        )
        parse_jobs = self.submit_parsing([target_id], label="resubmit-target-parsing")
        self.wait_for_stage(
            [target_id], "parsing", parse_jobs, label="wait-target-reparsing"
        )
        chunk_jobs = self.submit_stage(
            "chunking", [target_id], label="resubmit-target-chunking-after-blocks"
        )
        self.wait_for_stage(
            [target_id],
            "chunking",
            chunk_jobs,
            label="wait-target-rechunking-after-blocks",
        )
        vector_jobs = self.submit_stage(
            "vectorization", [target_id], label="resubmit-target-vectors-after-blocks"
        )
        self.wait_for_stage(
            [target_id],
            "vectorization",
            vector_jobs,
            label="wait-target-vectors-after-blocks",
        )
        graph_jobs = self.submit_stage(
            "graph-extraction", [target_id], label="resubmit-target-graph-after-blocks"
        )
        self.wait_for_stage(
            [target_id], "graph", graph_jobs, label="wait-target-graph-after-blocks"
        )

        delete_name = "doc-test-1.docx"
        delete_id = self.primary_documents[delete_name]["id"]
        self.request(
            "DELETE",
            f"{self.kb_root(self.primary_kb_id)}/documents/{delete_id}",
            expected=204,
            label="delete-document-and-derived-data",
        )
        self.request(
            "GET",
            f"{self.kb_root(self.primary_kb_id)}/documents/{delete_id}",
            expected=404,
            label="verify-document-deleted",
        )

    def run(self) -> None:
        fixtures = self.preflight()
        self.login_and_validate_capabilities()
        self.create_workspace_and_knowledge_bases()
        self.upload_primary(fixtures)
        self.verify_partial_upload()
        self.parse_and_configure_models()
        self.chunk_and_vectorize()
        self.graph_retrieval_and_merge()
        self.copy_and_compare()
        self.delete_and_reprocess()

    def cleanup(self) -> list[str]:
        if self.config.keep_resources:
            self.event("cleanup-skipped", resources=self.resources)
            return []
        errors: list[str] = []
        for kb_id in (self.copy_kb_id, self.primary_kb_id, self.isolation_kb_id):
            if not kb_id or not self.workspace_id:
                continue
            try:
                payload = self.json_request(
                    "DELETE",
                    f"{self.kb_root(kb_id)}",
                    expected=202,
                    label=f"cleanup-delete-knowledge-base-{kb_id}",
                )
                operation_id = payload.get("operation_id")
                require(
                    isinstance(operation_id, str),
                    "delete knowledge base returned no operation id",
                )
                self.wait_operation(
                    operation_id, label=f"cleanup-delete-knowledge-base-{kb_id}"
                )
            except (
                Exception
            ) as exc:  # Best effort while retaining full failure diagnostics.
                errors.append(f"knowledge base {kb_id}: {exc}")
        if self.workspace_id:
            try:
                self.request(
                    "DELETE",
                    f"/workspaces/{self.workspace_id}",
                    expected=204,
                    label="cleanup-delete-workspace",
                )
            except (
                Exception
            ) as exc:  # Workspace deletion is required for a clean successful run.
                errors.append(f"workspace {self.workspace_id}: {exc}")
        if errors:
            self.event("cleanup-failed", errors=errors, resources=self.resources)
        else:
            self.event("cleanup-completed", resources=self.resources)
        return errors

    def report(
        self, status: str, error: Exception | None, cleanup_errors: list[str]
    ) -> dict[str, Any]:
        return redact(
            {
                "status": status,
                "finished_at": datetime.now(UTC).isoformat(),
                "config": {
                    **asdict(self.config),
                    "password": "***",
                    "model_api_key": "***",
                    "files_dir": str(self.config.files_dir),
                    "report": str(self.config.report) if self.config.report else None,
                },
                "resources": self.resources,
                "error": str(error) if error else None,
                "cleanup_errors": cleanup_errors,
                "events": self.events,
            }
        )


def parse_args() -> Config:
    dotenv_values = parse_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gateway",
        default=configured_value("RAG_TEST_GATEWAY", dotenv_values)
        or gateway_from_env_file(),
        help="Gateway API base URL; defaults to GATEWAY_HOST/GATEWAY_PORT in .env.",
    )
    parser.add_argument(
        "--email", default=configured_value("RAG_TEST_EMAIL", dotenv_values)
    )
    parser.add_argument(
        "--password", default=configured_value("RAG_TEST_PASSWORD", dotenv_values)
    )
    parser.add_argument(
        "--files-dir",
        type=Path,
        default=Path(
            configured_value("RAG_TEST_FILES_DIR", dotenv_values) or DEFAULT_FILES_DIR
        ),
    )
    parser.add_argument(
        "--embedding-base-url",
        default=configured_value("RAG_TEST_EMBEDDING_BASE_URL", dotenv_values)
        or "http://127.0.0.1:31995/v1",
    )
    parser.add_argument(
        "--embedding-model",
        default=configured_value("RAG_TEST_EMBEDDING_MODEL", dotenv_values)
        or "Qwen3-Embedding-8B",
    )
    parser.add_argument(
        "--llm-base-url",
        default=configured_value("RAG_TEST_LLM_BASE_URL", dotenv_values)
        or "http://127.0.0.1:30018/v1",
    )
    parser.add_argument(
        "--llm-model",
        default=configured_value("RAG_TEST_LLM_MODEL", dotenv_values)
        or "Qwen3.8-27B-FP8",
    )
    parser.add_argument(
        "--model-api-key",
        default=configured_value("RAG_TEST_MODEL_API_KEY", dotenv_values) or "EMPTY",
    )
    parser.add_argument("--request-timeout-seconds", type=float, default=120)
    parser.add_argument("--job-timeout-seconds", type=float, default=1_800)
    parser.add_argument("--poll-interval-seconds", type=float, default=2)
    parser.add_argument("--keep-resources", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    require(bool(args.email), "required test configuration is missing: RAG_TEST_EMAIL")
    require(
        bool(args.password),
        "required test configuration is missing: RAG_TEST_PASSWORD",
    )
    require(args.request_timeout_seconds > 0, "request timeout must be positive")
    require(args.job_timeout_seconds > 0, "job timeout must be positive")
    require(args.poll_interval_seconds > 0, "poll interval must be positive")
    return Config(
        gateway=normalized_api_base(args.gateway),
        email=args.email,
        password=args.password,
        files_dir=args.files_dir.resolve(),
        embedding_base_url=normalized_api_base(args.embedding_base_url),
        embedding_model=args.embedding_model,
        llm_base_url=normalized_api_base(args.llm_base_url),
        llm_model=args.llm_model,
        model_api_key=args.model_api_key,
        request_timeout_seconds=args.request_timeout_seconds,
        job_timeout_seconds=args.job_timeout_seconds,
        poll_interval_seconds=args.poll_interval_seconds,
        keep_resources=args.keep_resources,
        report=args.report.resolve() if args.report else None,
    )


def main() -> int:
    try:
        config = parse_args()
    except FlowError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    flow = UserFlow(config)
    failure: Exception | None = None
    cleanup_errors: list[str] = []
    try:
        flow.run()
    except Exception as exc:
        failure = exc
    finally:
        try:
            cleanup_errors = flow.cleanup()
        finally:
            status = "passed" if failure is None and not cleanup_errors else "failed"
            report = flow.report(status, failure, cleanup_errors)
            if config.report:
                config.report.parent.mkdir(parents=True, exist_ok=True)
                config.report.write_text(
                    json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
                    + "\n",
                    encoding="utf-8",
                )
                print(f"Report: {config.report}")
            flow.close()
    if failure is not None:
        print(f"FAIL: {failure}", file=sys.stderr)
        if flow.resources:
            print(
                f"Created resources: {json.dumps(flow.resources, ensure_ascii=False)}",
                file=sys.stderr,
            )
        return 1
    if cleanup_errors:
        print("FAIL: cleanup did not complete:", file=sys.stderr)
        for error in cleanup_errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    if config.keep_resources:
        print(
            f"PASS: RAG user flow completed; resources retained: {json.dumps(flow.resources, ensure_ascii=False)}"
        )
    else:
        print("PASS: RAG user flow completed and created resources were cleaned up")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
