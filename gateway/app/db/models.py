import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, LargeBinary, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    metadata = MetaData(schema="platform")


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class User(Timestamped, Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(32), default="active")


class Workspace(Timestamped, Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_workspaces_user_name"),
        UniqueConstraint("user_id", "storage_key", name="uq_workspaces_user_storage_key"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    storage_key: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="active")
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


class ProviderBinding(Timestamped, Base):
    __tablename__ = "provider_bindings"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "provider_id", "display_name",
            name="uq_provider_bindings_workspace_provider_name",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id"), index=True)
    provider_id: Mapped[str] = mapped_column(String(128))
    display_name: Mapped[str] = mapped_column(String(128))
    base_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    nonce: Mapped[bytes] = mapped_column(LargeBinary(12))
    key_version: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(String(32), default="active")
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProviderBindingModel(Timestamped, Base):
    __tablename__ = "provider_binding_models"
    binding_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("provider_bindings.id", ondelete="CASCADE"), primary_key=True
    )
    model_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    context_window: Mapped[int | None] = mapped_column(nullable=True)
    max_tokens: Mapped[int | None] = mapped_column(nullable=True)
    reasoning: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


class AgentProfile(Timestamped, Base):
    __tablename__ = "agent_profiles"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    provider_binding_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("provider_bindings.id"))
    model_id: Mapped[str] = mapped_column(String(256))
    thinking_level: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AgentSession(Timestamped, Base):
    __tablename__ = "agent_sessions"
    __table_args__ = (
        CheckConstraint("total_tokens >= 0", name="ck_agent_sessions_total_tokens_nonnegative"),
        CheckConstraint("context_tokens >= 0", name="ck_agent_sessions_context_tokens_nonnegative"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    # Profiles are retained only as a legacy creation shortcut.  A Session owns
    # its current model configuration so it can change between completed runs.
    profile_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_profiles.id"), nullable=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id"))
    provider_binding_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("provider_bindings.id"), nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    thinking_level: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pi_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    pi_session_file_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="ready")
    title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    total_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    context_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))


class AgentSessionKnowledgeBase(Base):
    __tablename__ = "agent_session_knowledge_bases"
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("platform.agent_sessions.id", ondelete="CASCADE"), primary_key=True
    )
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rag.knowledge_bases.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (UniqueConstraint("session_id", "sequence", name="uq_chat_messages_session_sequence"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_sessions.id"), index=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_runs.id"), nullable=True, index=True)
    role: Mapped[str] = mapped_column(String(16))
    sequence: Mapped[int] = mapped_column()
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="completed")
    tool_call_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    tool_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    arguments: Mapped[object | None] = mapped_column(JSONB, nullable=True)
    result: Mapped[object | None] = mapped_column(JSONB, nullable=True)
    is_error: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    payload_truncated: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentRun(Base):
    __tablename__ = "agent_runs"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_sessions.id"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="running")
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_binding_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("provider_bindings.id"), nullable=True)
    provider_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    thinking_level: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

Index("uq_agent_runs_one_running_session", AgentRun.session_id, unique=True, postgresql_where=text("status = 'running'"))
Index("uq_workspaces_one_current", Workspace.user_id, unique=True, postgresql_where=text("is_current"))


class RuntimeInstance(Timestamped, Base):
    __tablename__ = "runtime_instances"
    __table_args__ = (UniqueConstraint("user_id"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    container_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    container_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    image: Mapped[str | None] = mapped_column(String(256), nullable=True)
    host_port: Mapped[int | None] = mapped_column(nullable=True)
    state: Mapped[str] = mapped_column(String(32), default="stopped")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rag_secret_digest: Mapped[bytes | None] = mapped_column(LargeBinary(32), nullable=True)
