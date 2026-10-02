"""add autodedup_reports (calibração do MEM0_AUTODEDUP_MODE=report)

Revision ID: s1t2u3v4w5x6
Revises: r0s1t2u3v4w5
Create Date: 2026-10-02 12:00:00.000000

Tabela nova e independente onde o modo ``report`` do autodedup grava cada par
candidato (nova memória x duplicata existente, score, limiar vigente, trechos
curtos). Serve só para calibrar ``MEM0_AUTODEDUP_THRESHOLD`` via
``GET /admin/autodedup/report``; nada nela é aplicado ao Qdrant.

Migração aditiva: cria uma tabela e seus índices, não altera nenhuma outra
tabela. Idempotente via inspect (mesma convenção de ``r0s1t2u3v4w5``).

Schema de desenvolvimento: uma versão anterior desta mesma revisão criava a
tabela com uma coluna ``mode NOT NULL`` que o modelo não preenche — todo INSERT
do relatório falharia (e o relatório engole a falha). Se ``autodedup_reports``
já existir com schema incompatível (falta coluna esperada, ou há coluna extra
``NOT NULL`` sem default), ela é descartada e recriada:
contém só pares de calibração, descartáveis e regeráveis com tráfego. Isso vale
apenas para ``autodedup_reports``; nenhuma outra tabela é inspecionada ou tocada.
O downgrade remove apenas a tabela criada aqui (só dados de relatório).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "s1t2u3v4w5x6"
down_revision: Union[str, None] = "r0s1t2u3v4w5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "autodedup_reports"
# Só os índices usados pelas consultas (ver app.models.AutodedupReport): o
# composto (project, created_at) já atende o filtro por projeto sozinho.
_INDEXES = (
    ("ix_autodedup_reports_created_at", ["created_at"]),
    ("ix_autodedup_reports_score", ["score"]),
    ("idx_autodedup_reports_project_time", ["project", "created_at"]),
)


# Colunas do schema final (iguais a app.models.AutodedupReport).
_COLUMNS = frozenset(
    {
        "id",
        "created_at",
        "job_id",
        "project",
        "new_memory_id",
        "duplicate_memory_id",
        "duplicate_project",
        "score",
        "threshold",
        "above_threshold",
        "new_text",
        "duplicate_text",
    }
)


def _drop_table(bind) -> None:
    """Remove ``autodedup_reports`` (e só ela) com seus índices."""
    for idx in sa.inspect(bind).get_indexes(_TABLE):
        if idx.get("name"):
            op.drop_index(idx["name"], table_name=_TABLE)
    op.drop_table(_TABLE)


def _incompatible(bind) -> bool:
    """Falta coluna esperada, ou há coluna extra NOT NULL sem default (ex.: ``mode``)
    — nesses casos os INSERTs do modelo falhariam."""
    cols = sa.inspect(bind).get_columns(_TABLE)
    names = {c["name"] for c in cols}
    if not _COLUMNS <= names:
        return True
    return any(
        c["name"] not in _COLUMNS and not c.get("nullable", True) and c.get("default") is None
        for c in cols
    )


def upgrade() -> None:
    bind = op.get_bind()
    if _TABLE in set(sa.inspect(bind).get_table_names()) and _incompatible(bind):
        # Schema de dev incompatível (ex.: ``mode NOT NULL``): recria só esta tabela.
        _drop_table(bind)
    if _TABLE not in set(sa.inspect(bind).get_table_names()):
        op.create_table(
            _TABLE,
            sa.Column("id", sa.UUID(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("job_id", sa.String(), nullable=True),
            sa.Column("project", sa.String(), nullable=True),
            sa.Column("new_memory_id", sa.String(), nullable=False),
            sa.Column("duplicate_memory_id", sa.String(), nullable=False),
            sa.Column("duplicate_project", sa.String(), nullable=True),
            sa.Column("score", sa.Float(), nullable=False),
            sa.Column("threshold", sa.Float(), nullable=False),
            sa.Column("above_threshold", sa.Boolean(), nullable=False),
            sa.Column("new_text", sa.String(), nullable=True),
            sa.Column("duplicate_text", sa.String(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )

    existing = {idx["name"] for idx in sa.inspect(bind).get_indexes(_TABLE)}
    for name, cols in _INDEXES:
        if name not in existing:
            op.create_index(name, _TABLE, cols)


def downgrade() -> None:
    bind = op.get_bind()
    if _TABLE not in set(sa.inspect(bind).get_table_names()):
        return
    existing = {idx["name"] for idx in sa.inspect(bind).get_indexes(_TABLE)}
    for name, _cols in reversed(_INDEXES):
        if name in existing:
            op.drop_index(name, table_name=_TABLE)
    op.drop_table(_TABLE)
