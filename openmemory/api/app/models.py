import datetime
import enum
import uuid

import sqlalchemy as sa
from app.database import Base
from app.utils.categorization import get_categories_for_memory
from sqlalchemy import (
    JSON,
    UUID,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    event,
)
from sqlalchemy.orm import Session, relationship


def get_current_utc_time():
    """Get current UTC time"""
    return datetime.datetime.now(datetime.UTC)


class MemoryState(enum.Enum):
    active = "active"
    paused = "paused"
    archived = "archived"
    deleted = "deleted"
    quarantined = "quarantined"


class WriteQueueStatus(enum.Enum):
    queued = "queued"
    processing = "processing"
    done = "done"
    skipped = "skipped"
    failed = "failed"


class GovernanceJobType(enum.Enum):
    dedup = "dedup"
    ttl_prune = "ttl_prune"
    consolidate = "consolidate"
    purge = "purge"
    # Fase 2 prontidão (task_04 / ADR-005): aplicação de teto por project e
    # arquivamento de projects inativos (cold tier).
    enforce_quota = "enforce_quota"
    cold_tier = "cold_tier"
    merge_projects = "merge_projects"
    # Avaliacao de qualidade da busca (job proprio, controlavel individualmente).
    quality_eval = "quality_eval"


class GovernanceJobStatus(enum.Enum):
    queued = "queued"
    processing = "processing"
    done = "done"
    failed = "failed"


class MigrationStatus(enum.Enum):
    """Lifecycle of the blue-green partition migration (task_01 / ADR-003)."""
    planned = "planned"
    copying = "copying"
    validating = "validating"
    flipped = "flipped"
    rolled_back = "rolled_back"
    done = "done"


class MachineStatus(enum.Enum):
    """Estado do vínculo máquina→pessoa (feature auth Google, ADR-004).

    ``unlinked``: máquina conhecida (legado ou nova) sem dono; ``linked``:
    vinculada a um usuário ``person``; ``conflict``: vínculo disputado por mais
    de uma conta — nunca resolvido automaticamente (tratamento na Fase 2).
    """
    unlinked = "unlinked"
    linked = "linked"
    conflict = "conflict"


class PartitionTier(enum.Enum):
    """Partition tier of a project in the shared Qdrant collection (ADR-002).

    ``shared`` projects live in the shared shard; ``dedicated`` projects have
    been promoted to their own custom shard key (task_08).
    """
    shared = "shared"
    dedicated = "dedicated"


class WriteQueueJob(Base):
    """Persistent write-queue row that decouples the MCP agent from LLM extraction.

    Each row represents a write job enqueued by ``add_memories`` and consumed by
    the background worker. Being SQLite-backed, jobs survive process restarts.

    NOTE: this is the ORM row model (table ``write_queue``). The queue *access
    layer* class is named ``WriteQueue`` and lives in ``app.utils.write_queue``.
    """
    __tablename__ = "write_queue"
    id = Column(UUID, primary_key=True, default=lambda: uuid.uuid4())
    project = Column(String, nullable=False, index=True)
    hostname = Column(String, nullable=False, index=True)
    client_name = Column(String, nullable=True)
    text = Column(Text, nullable=False)
    # Optional write options (e.g. {"supersedes": ["uuid", ...]}) from MCP add_memories.
    extras = Column(JSON, nullable=True)
    status = Column(Enum(WriteQueueStatus),
                    default=WriteQueueStatus.queued,
                    nullable=False,
                    index=True)
    error = Column(String, nullable=True)
    # Number of processing attempts already made; the worker retries a failed job
    # (re-queues it) until this reaches its configured ceiling, then marks it
    # terminally ``failed`` (task_06 / ADR-004).
    attempts = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=get_current_utc_time, index=True)
    updated_at = Column(DateTime,
                        default=get_current_utc_time,
                        onupdate=get_current_utc_time)

    __table_args__ = (
        Index('idx_write_queue_status_created', 'status', 'created_at'),
    )


class Project(Base):
    """Internal project catalog auto-managed by the memory.

    Spaces represent the company's projects (see ADR-002) and are auto-created
    and auto-cataloged on the first write of each project -- there is no manual
    administration. Each row is keyed by the project ``name`` and is materialized
    via the idempotent ``upsert_project`` helper (``app.utils.projects``), which
    the background worker (task_06) calls on the first write it processes for a
    given project.
    """
    __tablename__ = "projects"
    name = Column(String, primary_key=True)
    created_at = Column(DateTime, default=get_current_utc_time, index=True)
    first_seen_hostname = Column(String, nullable=True)
    memory_count = Column(Integer, nullable=True, default=0)
    # Última atividade de escrita/acesso do project (task_04 / ADR-005). Alimenta
    # a janela de inatividade do cold tier (task_07).
    last_activity_at = Column(DateTime, nullable=True, index=True)
    # Partitioning state (task_01 / ADR-002): every project starts ``shared`` and
    # may be promoted to a dedicated custom shard key (task_08).
    partition_tier = Column(
        Enum(PartitionTier),
        nullable=False,
        default=PartitionTier.shared,
        server_default=PartitionTier.shared.value,
    )
    shard_key = Column(String, nullable=True)


# Nome do grupo padrão ao qual usuários sem grupo explícito são associados
# (ADR-001/ADR-002). Mantido como constante para ser compartilhado entre o modelo,
# a migração e a lógica de grupo (fallback de leitura e gestão de membros).
DEFAULT_GROUP_NAME = "Default"


class Group(Base):
    """Equipe da empresa à qual usuários pertencem (ADR-002).

    Cada usuário pertence a no máximo um grupo (``users.group_id``). A associação
    memória→grupo é indireta e dinâmica: resolvida via o grupo atual do autor no
    momento da leitura, nunca persistida no payload do vetor.
    """

    __tablename__ = "groups"
    id = Column(UUID, primary_key=True, default=lambda: uuid.uuid4())
    name = Column(String, nullable=False, unique=True, index=True)
    created_at = Column(DateTime, default=get_current_utc_time, index=True)
    updated_at = Column(DateTime,
                        default=get_current_utc_time,
                        onupdate=get_current_utc_time)

    members = relationship("User", back_populates="group")


# Tipos de linha em ``users`` (ADR-004): ``legacy_host`` é a linha histórica cujo
# ``user_id`` guarda um hostname; ``person`` é a pessoa autenticada via Google,
# cujo ``user_id`` técnico é o ``google_sub``. String simples (não Enum nativo)
# para manter o ALTER TABLE aditivo trivial em ambos os dialetos.
USER_TYPE_PERSON = "person"
USER_TYPE_LEGACY_HOST = "legacy_host"


class User(Base):
    __tablename__ = "users"
    id = Column(UUID, primary_key=True, default=lambda: uuid.uuid4())
    user_id = Column(String, nullable=False, unique=True, index=True)
    name = Column(String, nullable=True, index=True)
    email = Column(String, unique=True, nullable=True, index=True)
    # FK nullable para retrocompatibilidade: usuários sem grupo explícito são
    # tratados como pertencentes ao grupo Default pela lógica de grupo (ADR-002).
    group_id = Column(UUID, ForeignKey("groups.id"), nullable=True, index=True)
    # Identidade Google (ADR-004): ``google_sub`` é o claim ``sub`` do ID token —
    # estável e nunca reutilizado; é a chave da pessoa (o e-mail é informativo).
    google_sub = Column(String, unique=True, nullable=True, index=True)
    display_name = Column(String, nullable=True)
    avatar_url = Column(String, nullable=True)
    user_type = Column(
        String,
        nullable=False,
        default=USER_TYPE_LEGACY_HOST,
        server_default=USER_TYPE_LEGACY_HOST,
        index=True,
    )
    metadata_ = Column('metadata', JSON, default=dict)
    created_at = Column(DateTime, default=get_current_utc_time, index=True)
    updated_at = Column(DateTime,
                        default=get_current_utc_time,
                        onupdate=get_current_utc_time)

    group = relationship("Group", back_populates="members")
    apps = relationship("App", back_populates="owner")
    memories = relationship("Memory", back_populates="user")


class Machine(Base):
    """Computador/host usado por uma pessoa (ADR-004/ADR-005).

    Uma linha por hostname. O vínculo ``linked_user_id`` → pessoa é a base da
    resolução dinâmica hostname→pessoa (as memórias no Qdrant continuam com o
    payload ``hostname`` intocado). ``legacy_user_id`` aponta a linha histórica
    de ``users`` cujo ``user_id`` é este hostname, quando existir.
    """

    __tablename__ = "machines"
    id = Column(UUID, primary_key=True, default=lambda: uuid.uuid4())
    hostname = Column(String, nullable=False, unique=True, index=True)
    linked_user_id = Column(UUID, ForeignKey("users.id"), nullable=True, index=True)
    legacy_user_id = Column(UUID, ForeignKey("users.id"), nullable=True, index=True)
    status = Column(
        Enum(MachineStatus),
        nullable=False,
        default=MachineStatus.unlinked,
        server_default=MachineStatus.unlinked.value,
        index=True,
    )
    linked_at = Column(DateTime, nullable=True)
    linked_by = Column(UUID, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=get_current_utc_time, index=True)
    updated_at = Column(DateTime,
                        default=get_current_utc_time,
                        onupdate=get_current_utc_time)

    linked_user = relationship("User", foreign_keys=[linked_user_id])
    legacy_user = relationship("User", foreign_keys=[legacy_user_id])


class AgentToken(Base):
    """Credencial dos agentes MCP de um usuário ``person`` (ADR-003/ADR-008).

    Gerado UMA única vez por conta, imutável e permanentemente exibível na tela
    de instalação (decisão de produto — ADR-008): o valor em claro fica em
    ``token_value``; o ``token_hash`` (SHA-256) continua sendo o índice usado
    pelo middleware para autenticar. ``revoked_at`` permanece como válvula de
    emergência administrativa (UPDATE direto no banco) — não há rotação via API.
    """

    __tablename__ = "agent_tokens"
    id = Column(UUID, primary_key=True, default=lambda: uuid.uuid4())
    user_id = Column(UUID, ForeignKey("users.id"), nullable=False, index=True)
    token_hash = Column(String, nullable=False, index=True)
    token_value = Column(String, nullable=True)
    prefix = Column(String, nullable=False)
    created_at = Column(DateTime, default=get_current_utc_time, index=True)
    revoked_at = Column(DateTime, nullable=True)
    last_used_at = Column(DateTime, nullable=True)

    user = relationship("User")

    __table_args__ = (
        Index(
            "uq_agent_tokens_active_user",
            "user_id",
            unique=True,
            postgresql_where=sa.text("revoked_at IS NULL"),
            sqlite_where=sa.text("revoked_at IS NULL"),
        ),
    )


class LinkAuditLog(Base):
    """Trilha auditável dos vínculos máquina↔pessoa (PRD "Regras de migração").

    Uma linha por transição (``link``, ``unlink``, ``conflict_detected``,
    ``conflict_resolved``) com o ator e detalhes em JSON.
    """

    __tablename__ = "link_audit_logs"
    id = Column(UUID, primary_key=True, default=lambda: uuid.uuid4())
    machine_id = Column(UUID, ForeignKey("machines.id"), nullable=False, index=True)
    actor_user_id = Column(UUID, ForeignKey("users.id"), nullable=True, index=True)
    action = Column(String, nullable=False, index=True)
    detail = Column(JSON, default=dict)
    created_at = Column(DateTime, default=get_current_utc_time, index=True)

    machine = relationship("Machine")
    actor = relationship("User")


class App(Base):
    __tablename__ = "apps"
    id = Column(UUID, primary_key=True, default=lambda: uuid.uuid4())
    owner_id = Column(UUID, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String, nullable=False, index=True)
    description = Column(String)
    metadata_ = Column('metadata', JSON, default=dict)
    is_active = Column(Boolean, default=True, index=True)
    created_at = Column(DateTime, default=get_current_utc_time, index=True)
    updated_at = Column(DateTime,
                        default=get_current_utc_time,
                        onupdate=get_current_utc_time)

    owner = relationship("User", back_populates="apps")
    memories = relationship("Memory", back_populates="app")

    __table_args__ = (
        sa.UniqueConstraint('owner_id', 'name', name='idx_app_owner_name'),
    )


class Config(Base):
    __tablename__ = "configs"
    id = Column(UUID, primary_key=True, default=lambda: uuid.uuid4())
    key = Column(String, unique=True, nullable=False, index=True)
    value = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=get_current_utc_time)
    updated_at = Column(DateTime,
                        default=get_current_utc_time,
                        onupdate=get_current_utc_time)


class Memory(Base):
    __tablename__ = "memories"
    id = Column(UUID, primary_key=True, default=lambda: uuid.uuid4())
    user_id = Column(UUID, ForeignKey("users.id"), nullable=False, index=True)
    app_id = Column(UUID, ForeignKey("apps.id"), nullable=False, index=True)
    content = Column(String, nullable=False)
    vector = Column(String)
    metadata_ = Column('metadata', JSON, default=dict)
    state = Column(Enum(MemoryState), default=MemoryState.active, index=True)
    created_at = Column(DateTime, default=get_current_utc_time, index=True)
    updated_at = Column(DateTime,
                        default=get_current_utc_time,
                        onupdate=get_current_utc_time)
    archived_at = Column(DateTime, nullable=True, index=True)
    deleted_at = Column(DateTime, nullable=True, index=True)
    quarantined_at = Column(DateTime, nullable=True, index=True)

    user = relationship("User", back_populates="memories")
    app = relationship("App", back_populates="memories")
    categories = relationship("Category", secondary="memory_categories", back_populates="memories")

    __table_args__ = (
        Index('idx_memory_user_state', 'user_id', 'state'),
        Index('idx_memory_app_state', 'app_id', 'state'),
        Index('idx_memory_user_app', 'user_id', 'app_id'),
    )


class Category(Base):
    __tablename__ = "categories"
    id = Column(UUID, primary_key=True, default=lambda: uuid.uuid4())
    name = Column(String, unique=True, nullable=False, index=True)
    description = Column(String)
    created_at = Column(DateTime, default=datetime.datetime.now(datetime.UTC), index=True)
    updated_at = Column(DateTime,
                        default=get_current_utc_time,
                        onupdate=get_current_utc_time)

    memories = relationship("Memory", secondary="memory_categories", back_populates="categories")

memory_categories = Table(
    "memory_categories", Base.metadata,
    Column("memory_id", UUID, ForeignKey("memories.id"), primary_key=True, index=True),
    Column("category_id", UUID, ForeignKey("categories.id"), primary_key=True, index=True),
    Index('idx_memory_category', 'memory_id', 'category_id')
)


class AccessControl(Base):
    __tablename__ = "access_controls"
    id = Column(UUID, primary_key=True, default=lambda: uuid.uuid4())
    subject_type = Column(String, nullable=False, index=True)
    subject_id = Column(UUID, nullable=True, index=True)
    object_type = Column(String, nullable=False, index=True)
    object_id = Column(UUID, nullable=True, index=True)
    effect = Column(String, nullable=False, index=True)
    created_at = Column(DateTime, default=get_current_utc_time, index=True)

    __table_args__ = (
        Index('idx_access_subject', 'subject_type', 'subject_id'),
        Index('idx_access_object', 'object_type', 'object_id'),
    )


class ArchivePolicy(Base):
    __tablename__ = "archive_policies"
    id = Column(UUID, primary_key=True, default=lambda: uuid.uuid4())
    criteria_type = Column(String, nullable=False, index=True)
    criteria_id = Column(UUID, nullable=True, index=True)
    days_to_archive = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=get_current_utc_time, index=True)

    __table_args__ = (
        Index('idx_policy_criteria', 'criteria_type', 'criteria_id'),
    )


class MemoryStatusHistory(Base):
    __tablename__ = "memory_status_history"
    id = Column(UUID, primary_key=True, default=lambda: uuid.uuid4())
    memory_id = Column(UUID, ForeignKey("memories.id"), nullable=False, index=True)
    changed_by = Column(UUID, ForeignKey("users.id"), nullable=False, index=True)
    old_state = Column(Enum(MemoryState), nullable=False, index=True)
    new_state = Column(Enum(MemoryState), nullable=False, index=True)
    changed_at = Column(DateTime, default=get_current_utc_time, index=True)

    __table_args__ = (
        Index('idx_history_memory_state', 'memory_id', 'new_state'),
        Index('idx_history_user_time', 'changed_by', 'changed_at'),
    )


class MemoryAccessLog(Base):
    __tablename__ = "memory_access_logs"
    id = Column(UUID, primary_key=True, default=lambda: uuid.uuid4())
    memory_id = Column(UUID, ForeignKey("memories.id"), nullable=False, index=True)
    app_id = Column(UUID, ForeignKey("apps.id"), nullable=False, index=True)
    accessed_at = Column(DateTime, default=get_current_utc_time, index=True)
    access_type = Column(String, nullable=False, index=True)
    metadata_ = Column('metadata', JSON, default=dict)

    __table_args__ = (
        Index('idx_access_memory_time', 'memory_id', 'accessed_at'),
        Index('idx_access_app_time', 'app_id', 'accessed_at'),
    )


class WriteAuditLog(Base):
    """Durable audit trail of write requests (task_04 / ADR-003).

    The shared-memory write path is asynchronous: ``add_memories`` enqueues a job
    and the memory is only materialized later by the background worker, so there
    is no ``memory_id`` to reference at request time (which is why the legacy
    ``MemoryAccessLog`` — FK-bound to ``memories`` — does not fit this flow).
    This table records *who* originated each write (hostname attribution) and the
    target project/client, keyed by the queue ``job_id``, so attribution is
    queryable and survives restarts without depending on log scraping.
    """
    __tablename__ = "write_audit_logs"
    id = Column(UUID, primary_key=True, default=lambda: uuid.uuid4())
    job_id = Column(UUID, nullable=True, index=True)
    project = Column(String, nullable=False, index=True)
    hostname = Column(String, nullable=False, index=True)
    client_name = Column(String, nullable=True)
    action = Column(String, nullable=False, default="enqueue", index=True)
    created_at = Column(DateTime, default=get_current_utc_time, index=True)

    __table_args__ = (
        Index('idx_write_audit_project_time', 'project', 'created_at'),
        Index('idx_write_audit_hostname_time', 'hostname', 'created_at'),
    )


class GovernanceJob(Base):
    """Persistent governance job queue (Fase 3 / ADR-002).

    Each row represents a background governance task (dedup, TTL prune,
    consolidation, purge) enqueued by the scheduler or admin API and consumed
    by ``governance-worker``.
    """
    __tablename__ = "governance_jobs"
    id = Column(UUID, primary_key=True, default=lambda: uuid.uuid4())
    job_type = Column(Enum(GovernanceJobType), nullable=False, index=True)
    project = Column(String, nullable=True, index=True)
    status = Column(
        Enum(GovernanceJobStatus),
        default=GovernanceJobStatus.queued,
        nullable=False,
        index=True,
    )
    attempts = Column(Integer, nullable=False, default=0)
    payload = Column(JSON, default=dict)
    error = Column(String, nullable=True)
    created_at = Column(DateTime, default=get_current_utc_time, index=True)
    updated_at = Column(
        DateTime,
        default=get_current_utc_time,
        onupdate=get_current_utc_time,
    )

    __table_args__ = (
        Index("idx_governance_jobs_status_created", "status", "created_at"),
    )


class GovernancePolicy(Base):
    """Per-project override of the global governance policy (ADR-005)."""
    __tablename__ = "governance_policies"
    project_name = Column(String, ForeignKey("projects.name"), primary_key=True)
    overrides = Column(JSON, nullable=False, default=dict)
    updated_at = Column(
        DateTime,
        default=get_current_utc_time,
        onupdate=get_current_utc_time,
    )


class GovernanceSchedule(Base):
    """Last scheduled run per job type and scope (idempotent scheduling)."""
    __tablename__ = "governance_schedule"
    job_type = Column(Enum(GovernanceJobType), primary_key=True)
    scope = Column(String, primary_key=True)
    last_run_at = Column(DateTime, nullable=True, index=True)


class MigrationState(Base):
    """Global state of the blue-green partition migration (task_01 / ADR-003).

    A single logical row tracks the source (blue) and target (green) collections,
    which collection is currently served (``active_collection`` — the flip
    pointer), whether the write path is mirroring to the target
    (``dual_write_enabled``), and the copy checkpoint (``scroll_cursor``) used by
    the dedicated migration worker (task_06) to resume idempotently.
    """
    __tablename__ = "migration_state"
    id = Column(Integer, primary_key=True, autoincrement=True)
    source_collection = Column(String, nullable=False)
    target_collection = Column(String, nullable=False)
    active_collection = Column(String, nullable=False)
    dual_write_enabled = Column(
        Boolean, nullable=False, default=False, server_default=sa.false()
    )
    scroll_cursor = Column(String, nullable=True)
    status = Column(
        Enum(MigrationStatus),
        nullable=False,
        default=MigrationStatus.planned,
        server_default=MigrationStatus.planned.value,
    )
    updated_at = Column(
        DateTime, default=get_current_utc_time, onupdate=get_current_utc_time
    )


class TokenUsageLog(Base):
    """Registro de consumo de tokens por chamada LLM/embedding (métricas).

    Uma linha por chamada instrumentada ao backend de IA (extração, update,
    embedding de busca/gravação). A atribuição (project/agent/user) vem do
    contexto da operação em curso (ver ``app.utils.token_usage_wrapper``);
    ``operation_type`` é a operação de memória (add, search, update, embed).
    Falhas também são registradas (``success=False`` + ``error``) para que a
    cobertura de instrumentação seja auditável.
    """

    __tablename__ = "token_usage_logs"
    id = Column(UUID, primary_key=True, default=lambda: uuid.uuid4())
    created_at = Column(
        DateTime, nullable=False, default=get_current_utc_time, index=True
    )
    project = Column(String, nullable=False, index=True)
    agent = Column(String, nullable=False, index=True)
    user_id = Column(String, nullable=False, index=True)
    operation_type = Column(String, nullable=False, index=True)
    model = Column(String, nullable=False, index=True)
    input_tokens = Column(Integer, nullable=False, default=0)
    output_tokens = Column(Integer, nullable=False, default=0)
    total_tokens = Column(Integer, nullable=False, default=0)
    cache_read_tokens = Column(Integer, nullable=False, default=0)
    cache_write_tokens = Column(Integer, nullable=False, default=0)
    duration_ms = Column(BigInteger, nullable=True)
    success = Column(Boolean, nullable=False, default=True)
    error = Column(Text, nullable=True)
    trace_id = Column(String, nullable=True)

    __table_args__ = (
        Index("idx_token_usage_project_time", "project", "created_at"),
        Index("idx_token_usage_agent_time", "agent", "created_at"),
        Index("idx_token_usage_user_time", "user_id", "created_at"),
    )


# ---------------------------------------------------------------------------
# Espaço compartilhado de especificações (shared-specs / task_01 / ADR-004)
#
# Hierarquia ``Project → SpecWorkspace → (SpecDocument, TaskCard)``. As classes
# abaixo seguem a mesma convenção do restante do arquivo (id ``UUID`` com
# ``default=lambda: uuid.uuid4()``, par ``created_at``/``updated_at`` com
# ``get_current_utc_time``) e espelham ``MemoryStatusHistory`` (histórico) e
# ``LinkAuditLog`` (auditoria). O controle de acesso reaproveita ``AccessControl``
# com ``object_type="spec_workspace"`` — sem tabela nova (ADR-004).
# ---------------------------------------------------------------------------


class SpecWorkspaceStatus(enum.Enum):
    """Ciclo de vida de um ``SpecWorkspace`` (a "Tarefa" do PRD)."""
    planejamento = "planejamento"
    ativo = "ativo"
    concluido = "concluido"
    arquivado = "arquivado"


class TaskCardStatus(enum.Enum):
    """Colunas do quadro Kanban (ADR-001/ADR-007).

    ``tasks`` = disponível/backlog. O estado "bloqueado" NÃO é um status: é
    representado pelos campos ortogonais ``is_blocked``/``block_reason`` em
    ``TaskCard``, mantendo a coluna atual do card.
    """
    tasks = "tasks"
    em_andamento = "em_andamento"
    revisao_codigo = "revisao_codigo"
    fase_teste = "fase_teste"
    concluido = "concluido"


class DocumentType(enum.Enum):
    """Tipo de documento de spec dentro de um workspace.

    ``adrs`` = documento versionado com o texto completo dos ADRs do workspace
    (não é TaskCard/Kanban). Alias MCP ``adr`` → ``adrs``.
    """
    prd = "prd"
    techspec = "techspec"
    tasks = "tasks"
    adrs = "adrs"


def parse_document_type(raw: str) -> DocumentType:
    """Resolve string MCP/API para ``DocumentType`` (aceita alias ``adr``).

    Fonte autoritativa dos tipos aceitos. Em entrada inválida, a mensagem lista os
    válidos — ``DocumentType(key)`` sozinho diria apenas que o valor não serve, sem
    dizer o que serviria.
    """
    key = (raw or "").strip().lower()
    if key in ("adr", "adrs"):
        return DocumentType.adrs
    try:
        return DocumentType(key)
    except ValueError:
        raise ValueError(
            f"document_type inválido: {raw!r}. "
            f"Válidos: {', '.join(t.value for t in DocumentType)} (alias: adr → adrs)"
        ) from None


class DocumentOrigin(enum.Enum):
    """Origem de uma gravação/ação de spec (MCP, UI ou REST)."""
    mcp = "mcp"
    ui = "ui"
    api = "api"


class CommentTargetType(enum.Enum):
    """Alvo polimórfico de um ``SpecComment``."""
    workspace = "workspace"
    document = "document"
    task = "task"


class SpecWorkspace(Base):
    """Espaço de trabalho de specs de uma Tarefa, filho de ``Project`` (ADR-004).

    Uma Tarefa é sempre filha de um Projeto (1:N). ``project_id`` referencia a
    PK textual de ``projects`` (``projects.name``) — o catálogo estável já usado
    para filtrar memórias — sem estender ou substituir ``Project``.
    """
    __tablename__ = "spec_workspaces"
    id = Column(UUID, primary_key=True, default=lambda: uuid.uuid4())
    project_id = Column(String, ForeignKey("projects.name"), nullable=False, index=True)
    slug = Column(String, nullable=False, index=True)
    name = Column(String, nullable=False)
    status = Column(
        Enum(SpecWorkspaceStatus),
        nullable=False,
        default=SpecWorkspaceStatus.planejamento,
        server_default=SpecWorkspaceStatus.planejamento.value,
        index=True,
    )
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=get_current_utc_time, index=True)
    updated_at = Column(DateTime,
                        default=get_current_utc_time,
                        onupdate=get_current_utc_time)
    # Lifecycle de arquivamento (Tarefa kanban-archive-lifecycle). Nullable: um
    # workspace nunca concluído/arquivado nunca preenche estes campos.
    # ``completed_at`` é a referência do worker de auto-arquivamento (1 mês após
    # concluído) — não reaproveita ``updated_at`` porque este é sobrescrito por
    # qualquer edição, não só pela transição de status.
    completed_at = Column(DateTime, nullable=True)
    archived_at = Column(DateTime, nullable=True)
    archived_by = Column(String, nullable=True)
    # Grupo dono do workspace (isolation por grupo, kanban-board-group-isolation).
    # Gravado na criação a partir do grupo do criador e imutável depois.
    # NULL = sem grupo resolvido: fail-closed, invisível para todos, nunca Default.
    group_id = Column(UUID, ForeignKey("groups.id"), nullable=True, index=True)

    project = relationship("Project")
    documents = relationship("SpecDocument", back_populates="workspace")
    tasks = relationship("TaskCard", back_populates="workspace")
    group = relationship("Group")

    __table_args__ = (
        sa.UniqueConstraint("project_id", "slug", name="uq_spec_workspace_project_slug"),
    )


class SpecDocument(Base):
    """Documento de spec (PRD/TechSpec/Tasks) — um por tipo dentro do workspace.

    ``current_version`` é um inteiro monotônico incrementado a cada gravação
    bem-sucedida (base do controle de concorrência otimista — ADR-005) e
    ``current_content`` guarda uma cópia da versão vigente para leitura rápida;
    o histórico completo por snapshot vive em ``SpecDocumentVersion``.
    """
    __tablename__ = "spec_documents"
    id = Column(UUID, primary_key=True, default=lambda: uuid.uuid4())
    workspace_id = Column(UUID, ForeignKey("spec_workspaces.id"), nullable=False, index=True)
    document_type = Column(Enum(DocumentType), nullable=False)
    # 0 = documento criado mas sem versão gravada ainda; a primeira gravação
    # (``expected_version=None``) o leva a 1 (ver ``app.utils.spec_versioning``).
    current_version = Column(Integer, nullable=False, default=0, server_default="0")
    current_content = Column(Text, nullable=True)
    updated_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=get_current_utc_time, index=True)
    updated_at = Column(DateTime,
                        default=get_current_utc_time,
                        onupdate=get_current_utc_time)

    workspace = relationship("SpecWorkspace", back_populates="documents")
    versions = relationship("SpecDocumentVersion", back_populates="document")

    __table_args__ = (
        sa.UniqueConstraint("workspace_id", "document_type", name="uq_spec_document_workspace_type"),
    )


class SpecDocumentVersion(Base):
    """Snapshot completo de uma versão de ``SpecDocument`` (ADR-004).

    Cada gravação insere uma nova linha com o conteúdo completo, nunca
    sobrescrevendo a anterior. ``(document_id, version)`` é único.
    """
    __tablename__ = "spec_document_versions"
    id = Column(UUID, primary_key=True, default=lambda: uuid.uuid4())
    document_id = Column(UUID, ForeignKey("spec_documents.id"), nullable=False, index=True)
    version = Column(Integer, nullable=False)
    content = Column(Text, nullable=False)
    author = Column(String, nullable=True)
    origin = Column(Enum(DocumentOrigin), nullable=False)
    created_at = Column(DateTime, default=get_current_utc_time, index=True)

    document = relationship("SpecDocument", back_populates="versions")

    __table_args__ = (
        sa.UniqueConstraint("document_id", "version", name="uq_spec_version_document_version"),
    )


class TaskCard(Base):
    """Card de tarefa técnica do quadro Kanban (ADR-001/ADR-007).

    ``status`` é a coluna atual (``tasks``=backlog). ``is_blocked``/``block_reason``
    são ortogonais à coluna. ``version`` é o inteiro monotônico usado para
    impedir que uma task em andamento seja reivindicada por outro ator (ADR-005).
    """
    __tablename__ = "task_cards"
    id = Column(UUID, primary_key=True, default=lambda: uuid.uuid4())
    workspace_id = Column(UUID, ForeignKey("spec_workspaces.id"), nullable=False, index=True)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    status = Column(
        Enum(TaskCardStatus),
        nullable=False,
        default=TaskCardStatus.tasks,
        server_default=TaskCardStatus.tasks.value,
        index=True,
    )
    is_blocked = Column(Boolean, nullable=False, default=False, server_default=sa.false())
    block_reason = Column(Text, nullable=True)
    assignee = Column(String, nullable=True, index=True)
    version = Column(Integer, nullable=False, default=1, server_default="1")
    last_activity_at = Column(DateTime, nullable=True, index=True)
    branch_ref = Column(Text, nullable=True)
    # Campos ricos do Kanban (kanban-planka / ADR-005) — Spec continua SoT.
    due_at = Column(DateTime, nullable=True, index=True)
    position = Column(sa.Float, nullable=False, default=65536.0, server_default="65536")
    # Arquivamento não destrutivo (alternativa ao delete_task): o card sai da
    # listagem padrão mas mantém coluna, histórico de status e comentários.
    # ``archived_at`` nulo = card ativo. Ortogonal a ``status``.
    archived_at = Column(DateTime, nullable=True, index=True)
    archived_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=get_current_utc_time, index=True)
    updated_at = Column(DateTime,
                        default=get_current_utc_time,
                        onupdate=get_current_utc_time)

    workspace = relationship("SpecWorkspace", back_populates="tasks")

    __table_args__ = (
        Index("idx_task_card_workspace_status", "workspace_id", "status"),
    )


class TaskStatusHistory(Base):
    """Histórico de mudança de coluna de uma ``TaskCard`` (espelha ``MemoryStatusHistory``).

    ``changed_by`` é o identificador textual do ator (hostname do agente ou
    e-mail do usuário), coerente com o restante do domínio de specs.
    """
    __tablename__ = "task_status_history"
    id = Column(UUID, primary_key=True, default=lambda: uuid.uuid4())
    task_id = Column(UUID, ForeignKey("task_cards.id"), nullable=False, index=True)
    old_status = Column(Enum(TaskCardStatus), nullable=False, index=True)
    new_status = Column(Enum(TaskCardStatus), nullable=False, index=True)
    changed_by = Column(String, nullable=True, index=True)
    changed_at = Column(DateTime, default=get_current_utc_time, index=True)

    task = relationship("TaskCard")

    __table_args__ = (
        Index("idx_task_history_task_status", "task_id", "new_status"),
        Index("idx_task_history_actor_time", "changed_by", "changed_at"),
    )


class SpecAuditLog(Base):
    """Trilha auditável de ações no espaço de specs (espelha ``LinkAuditLog``).

    Uma linha por ação (``action`` livre) com o ator, ``detail`` em JSON e a
    ``origin`` (MCP/UI/API). Reaproveitável diretamente como fonte de
    observabilidade.
    """
    __tablename__ = "spec_audit_logs"
    id = Column(UUID, primary_key=True, default=lambda: uuid.uuid4())
    workspace_id = Column(UUID, ForeignKey("spec_workspaces.id"), nullable=False, index=True)
    actor = Column(String, nullable=True, index=True)
    action = Column(String, nullable=False, index=True)
    detail = Column(JSON, default=dict)
    origin = Column(Enum(DocumentOrigin), nullable=True)
    created_at = Column(DateTime, default=get_current_utc_time, index=True)

    workspace = relationship("SpecWorkspace")


class SpecComment(Base):
    """Comentário em workspace/documento/task (referência polimórfica).

    ``target_id`` referencia a PK do alvo indicado por ``target_type`` sem FK
    física (polimórfico), à semelhança de ``AccessControl.object_id``.
    """
    __tablename__ = "spec_comments"
    id = Column(UUID, primary_key=True, default=lambda: uuid.uuid4())
    target_type = Column(Enum(CommentTargetType), nullable=False, index=True)
    target_id = Column(UUID, nullable=False, index=True)
    author = Column(String, nullable=True)
    body = Column(Text, nullable=False)
    created_at = Column(DateTime, default=get_current_utc_time, index=True)

    __table_args__ = (
        Index("idx_spec_comment_target", "target_type", "target_id"),
    )


class TaskLabel(Base):
    """Etiqueta de workspace (Kanban rico — Spec SoT)."""
    __tablename__ = "task_labels"
    id = Column(UUID, primary_key=True, default=lambda: uuid.uuid4())
    workspace_id = Column(UUID, ForeignKey("spec_workspaces.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    color = Column(String, nullable=True)
    created_at = Column(DateTime, default=get_current_utc_time)

    __table_args__ = (
        sa.UniqueConstraint("workspace_id", "name", name="uq_task_label_workspace_name"),
    )


class TaskCardLabel(Base):
    """Associação N:N card ↔ label."""
    __tablename__ = "task_card_labels"
    task_id = Column(UUID, ForeignKey("task_cards.id"), primary_key=True)
    label_id = Column(UUID, ForeignKey("task_labels.id"), primary_key=True)


class TaskChecklist(Base):
    __tablename__ = "task_checklists"
    id = Column(UUID, primary_key=True, default=lambda: uuid.uuid4())
    task_id = Column(UUID, ForeignKey("task_cards.id"), nullable=False, index=True)
    title = Column(String, nullable=False, default="Checklist")
    position = Column(sa.Float, nullable=False, default=65536.0, server_default="65536")
    created_at = Column(DateTime, default=get_current_utc_time)


class TaskChecklistItem(Base):
    __tablename__ = "task_checklist_items"
    id = Column(UUID, primary_key=True, default=lambda: uuid.uuid4())
    checklist_id = Column(UUID, ForeignKey("task_checklists.id"), nullable=False, index=True)
    title = Column(String, nullable=False)
    is_completed = Column(Boolean, nullable=False, default=False, server_default=sa.false())
    position = Column(sa.Float, nullable=False, default=65536.0, server_default="65536")
    created_at = Column(DateTime, default=get_current_utc_time)


class TaskAttachment(Base):
    """Metadados de anexo; bytes em volume/MinIO (nunca Qdrant)."""
    __tablename__ = "task_attachments"
    id = Column(UUID, primary_key=True, default=lambda: uuid.uuid4())
    task_id = Column(UUID, ForeignKey("task_cards.id"), nullable=False, index=True)
    filename = Column(String, nullable=False)
    content_type = Column(String, nullable=True)
    size_bytes = Column(sa.BigInteger, nullable=False, default=0, server_default="0")
    storage_key = Column(String, nullable=False)
    uploaded_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=get_current_utc_time)


class TaskMember(Base):
    """Membros adicionais além de ``TaskCard.assignee``."""
    __tablename__ = "task_members"
    id = Column(UUID, primary_key=True, default=lambda: uuid.uuid4())
    task_id = Column(UUID, ForeignKey("task_cards.id"), nullable=False, index=True)
    member = Column(String, nullable=False)
    created_at = Column(DateTime, default=get_current_utc_time)

    __table_args__ = (
        sa.UniqueConstraint("task_id", "member", name="uq_task_member"),
    )


class KanbanColumnPrompt(Base):
    """Prompts de especificação por coluna do pipeline Kanban."""
    __tablename__ = "kanban_column_prompts"
    column_status = Column(String(100), primary_key=True, nullable=False)
    prompt = Column(Text, nullable=False)
    is_enabled = Column(Boolean, nullable=False, default=True, server_default=sa.true())
    updated_at = Column(
        DateTime,
        default=get_current_utc_time,
        onupdate=get_current_utc_time,
        nullable=False,
        server_default=sa.func.now(),
    )
    updated_by = Column(String(255), nullable=True)

    __table_args__ = (
        sa.CheckConstraint("LENGTH(prompt) <= 5000", name="chk_kanban_column_prompt_length"),
        {"comment": "Prompts de especificação por coluna do pipeline Kanban"},
    )


class SpecPlankaIdMap(Base):
    """Correlação UUID Spec ↔ snowflake PLANKA (ADR-005 / kanban-planka).

    ``entity_type`` identifica o tipo espelhado (``project``, ``board``,
    ``list:<status>``, ``task``, ``document``). ``spec_id`` é o UUID Spec
    (workspace/task/document); para listas usa o ``workspace_id``.
    """
    __tablename__ = "spec_planka_id_map"
    id = Column(UUID, primary_key=True, default=lambda: uuid.uuid4())
    entity_type = Column(String, nullable=False, index=True)
    spec_id = Column(UUID, nullable=False, index=True)
    planka_id = Column(String, nullable=False, index=True)
    created_at = Column(DateTime, default=get_current_utc_time)
    updated_at = Column(DateTime,
                        default=get_current_utc_time,
                        onupdate=get_current_utc_time)

    __table_args__ = (
        sa.UniqueConstraint("entity_type", "spec_id", name="uq_spec_planka_entity_spec"),
        sa.UniqueConstraint("entity_type", "planka_id", name="uq_spec_planka_entity_planka"),
    )


def categorize_memory(memory: Memory, db: Session) -> None:
    """Categorize a memory using OpenAI and store the categories in the database."""
    try:
        # Get categories from OpenAI
        categories = get_categories_for_memory(memory.content)

        # Get or create categories in the database
        for category_name in categories:
            category = db.query(Category).filter(Category.name == category_name).first()
            if not category:
                category = Category(
                    name=category_name,
                    description=f"Automatically created category for {category_name}"
                )
                db.add(category)
                db.flush()  # Flush to get the category ID

            # Check if the memory-category association already exists
            existing = db.execute(
                memory_categories.select().where(
                    (memory_categories.c.memory_id == memory.id) &
                    (memory_categories.c.category_id == category.id)
                )
            ).first()

            if not existing:
                # Create the association
                db.execute(
                    memory_categories.insert().values(
                        memory_id=memory.id,
                        category_id=category.id
                    )
                )

        db.commit()
    except Exception as e:
        db.rollback()
        print(f"Error categorizing memory: {e}")


@event.listens_for(Memory, 'after_insert')
def after_memory_insert(mapper, connection, target):
    """Trigger categorization after a memory is inserted."""
    db = Session(bind=connection)
    categorize_memory(target, db)
    db.close()


@event.listens_for(Memory, 'after_update')
def after_memory_update(mapper, connection, target):
    """Trigger categorization after a memory is updated."""
    db = Session(bind=connection)
    categorize_memory(target, db)
    db.close()
