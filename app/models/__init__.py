import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    literal_column,
)
from sqlalchemy.sql import func

from ..database import Base


class ForgeProject(Base):
    __tablename__ = "forge_projects"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, nullable=False)
    repo_path = Column(String)
    description = Column(Text)
    status = Column(String, nullable=False, default='active')
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

class ForgeGoal(Base):
    __tablename__ = "forge_goals"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = Column(String, ForeignKey("forge_projects.id"), nullable=False)
    title = Column(String, nullable=False)
    description = Column(Text)
    success_criteria = Column(JSON, nullable=False, default=list)
    status = Column(String, nullable=False, default='active')
    priority = Column(Integer, nullable=False, default=3)
    embedding = Column(Vector(768), nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

class ForgeTask(Base):
    __tablename__ = "forge_tasks"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = Column(String, ForeignKey("forge_projects.id"), nullable=False)
    goal_id = Column(String, ForeignKey("forge_goals.id"), nullable=False)
    parent_task_id = Column(String, ForeignKey("forge_tasks.id"), nullable=True)
    title = Column(String, nullable=False)
    description = Column(Text)
    status = Column(String, nullable=False)
    priority = Column(Integer, nullable=False, default=3)
    acceptance_criteria = Column(JSON, nullable=False, default=list)
    verification_required = Column(Boolean, nullable=False, default=True)
    attempt_count = Column(Integer, nullable=False, default=0)
    no_progress_count = Column(Integer, nullable=False, default=0)
    evidence_baseline_at = Column(DateTime, nullable=True)
    last_progress_at = Column(DateTime, nullable=True)
    embedding = Column(Vector(768), nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

class ForgeEvent(Base):
    __tablename__ = "forge_events"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = Column(String, ForeignKey("forge_projects.id"), nullable=False)
    task_id = Column(String, ForeignKey("forge_tasks.id"), nullable=True)
    event_type = Column(String, nullable=False)
    actor = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    event_metadata = Column("metadata", JSON, nullable=False, default={})
    created_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        Index(
            'idx_forge_events_content_tsvector',
            func.to_tsvector(literal_column("'english'"), content),
            postgresql_using='gin'
        ),
    )

class ForgeMemoryItem(Base):
    __tablename__ = "forge_memory_items"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = Column(String, ForeignKey("forge_projects.id"), nullable=False)
    task_id = Column(String, ForeignKey("forge_tasks.id"), nullable=True)
    source_event_id = Column(String, ForeignKey("forge_events.id"), nullable=True)
    memory_type = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    status = Column(String, nullable=False, default='active')
    confidence = Column(Numeric, nullable=False, default=0.8)
    importance = Column(Integer, nullable=False, default=3)
    tags = Column(ARRAY(String), nullable=False, default=list)
    file_path = Column(String)
    supersedes_id = Column(String, ForeignKey("forge_memory_items.id"), nullable=True)
    embedding = Column(Vector(768), nullable=True)
    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index(
            'idx_forge_memory_items_content_tsvector',
            func.to_tsvector(literal_column("'english'"), content),
            postgresql_using='gin'
        ),
    )

class ForgeFileChange(Base):
    __tablename__ = "forge_file_changes"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = Column(String, ForeignKey("forge_projects.id"), nullable=False)
    task_id = Column(String, ForeignKey("forge_tasks.id"), nullable=True)
    file_path = Column(String, nullable=False)
    change_summary = Column(Text, nullable=False)
    reason = Column(Text)
    created_at = Column(DateTime, server_default=func.now())

class ForgeTestRun(Base):
    __tablename__ = "forge_test_runs"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = Column(String, ForeignKey("forge_projects.id"), nullable=False)
    task_id = Column(String, ForeignKey("forge_tasks.id"), nullable=True)
    command = Column(String, nullable=False)
    status = Column(String, nullable=False)
    output_summary = Column(Text)
    failure_summary = Column(Text)
    created_at = Column(DateTime, server_default=func.now())

class ForgeCheckpoint(Base):
    __tablename__ = "forge_checkpoints"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = Column(String, ForeignKey("forge_projects.id"), nullable=False)
    goal_id = Column(String, ForeignKey("forge_goals.id"), nullable=True)
    task_id = Column(String, ForeignKey("forge_tasks.id"), nullable=True)
    summary = Column(Text, nullable=False)
    current_state = Column(JSON, nullable=False, default={})
    next_actions = Column(JSON, nullable=False, default=list)
    open_risks = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime, server_default=func.now())

class ForgeContextPackLog(Base):
    __tablename__ = "forge_context_pack_logs"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = Column(String, ForeignKey("forge_projects.id"), nullable=False)
    task_id = Column(String, ForeignKey("forge_tasks.id"), nullable=True)
    query = Column(String)
    selected_memory_ids = Column(ARRAY(String), nullable=False, default=list)
    token_estimate = Column(Integer)
    created_at = Column(DateTime, server_default=func.now())


class ForgeContextCompressionSnapshot(Base):
    """Reversible Headroom compression record stored in canonical Postgres."""
    __tablename__ = "forge_context_compression_snapshots"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = Column(String, ForeignKey("forge_projects.id", ondelete="CASCADE"), nullable=False)
    goal_id = Column(String, ForeignKey("forge_goals.id", ondelete="SET NULL"), nullable=True)
    task_id = Column(String, ForeignKey("forge_tasks.id", ondelete="SET NULL"), nullable=True)
    source_hash = Column(String, nullable=False)
    source_memory_ids = Column(ARRAY(String), nullable=False, default=list)
    raw_context = Column(JSON, nullable=False)
    compressed_context = Column(JSON, nullable=False)
    tokens_before = Column(Integer, nullable=False, default=0)
    tokens_after = Column(Integer, nullable=False, default=0)
    tokens_saved = Column(Integer, nullable=False, default=0)
    compression_ratio = Column(Numeric, nullable=False, default=0)
    transforms_applied = Column(ARRAY(String), nullable=False, default=list)
    compressor = Column(String, nullable=False, default="headroom-ai")
    compressor_version = Column(String, nullable=False)
    created_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        Index("idx_context_compression_scope_hash", project_id, goal_id, task_id, source_hash),
    )

class ForgeSession(Base):
    __tablename__ = "sessions"

    id = Column(String, primary_key=True)
    source = Column(String)
    user_id = Column(String)
    model = Column(String)
    model_config = Column(Text)
    system_prompt = Column(Text)
    parent_session_id = Column(String)
    started_at = Column(Numeric)
    ended_at = Column(Numeric)
    end_reason = Column(String)
    message_count = Column(Integer)
    tool_call_count = Column(Integer)
    input_tokens = Column(Integer)
    output_tokens = Column(Integer)
    cache_read_tokens = Column(Integer)
    cache_write_tokens = Column(Integer)
    reasoning_tokens = Column(Integer)
    billing_provider = Column(String)
    billing_base_url = Column(String)
    billing_mode = Column(String)
    estimated_cost_usd = Column(Numeric)
    actual_cost_usd = Column(Numeric)
    cost_status = Column(String)
    cost_source = Column(String)
    pricing_version = Column(String)
    title = Column(String)
    api_call_count = Column(Integer)
    handoff_state = Column(Text)
    handoff_platform = Column(String)
    handoff_error = Column(Text)
    cwd = Column(Text)
    rewind_count = Column(Integer)
    archived = Column(Integer)


class ForgeRuntimeMetadata(Base):
    __tablename__ = "forge_runtime_metadata"

    key = Column(String, primary_key=True)
    value = Column(Text)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

class ForgeMessage(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True)
    session_id = Column(String)
    role = Column(String)
    content = Column(Text)
    tool_call_id = Column(String)
    tool_calls = Column(Text)
    tool_name = Column(String)
    timestamp = Column(Numeric)
    token_count = Column(Integer)
    finish_reason = Column(String)
    reasoning = Column(Text)
    reasoning_content = Column(Text)
    reasoning_details = Column(Text)
    codex_reasoning_items = Column(Text)
    codex_message_items = Column(Text)
    platform_message_id = Column(String)
    observed = Column(Integer)
    active = Column(Integer)

    __table_args__ = (
        Index(
            "idx_messages_content_tsvector",
            func.to_tsvector(literal_column("'english'"), content),
            postgresql_using="gin",
        ),
        Index("idx_messages_session_timestamp", session_id, timestamp),
    )


# One-release compatibility aliases for external readers. Internal code should
# migrate to Forge* names; the aliases are intentionally kept out of metadata.
HermesProject = ForgeProject
HermesGoal = ForgeGoal
HermesTask = ForgeTask
HermesEvent = ForgeEvent
HermesMemoryItem = ForgeMemoryItem
HermesFileChange = ForgeFileChange
HermesTestRun = ForgeTestRun
HermesCheckpoint = ForgeCheckpoint
HermesContextPackLog = ForgeContextPackLog
HermesContextCompressionSnapshot = ForgeContextCompressionSnapshot
HermesSession = ForgeSession
HermesRuntimeMetadata = ForgeRuntimeMetadata
HermesMessage = ForgeMessage
