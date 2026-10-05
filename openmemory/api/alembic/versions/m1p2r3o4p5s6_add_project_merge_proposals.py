"""add project_merge_proposals table

Revision ID: m1p2r3o4p5s6
Revises: r0s1t2u3v4w5
Create Date: 2026-10-02 00:00:00.000000

Tabela dedicada às propostas de unificação de projetos (antes guardadas como
JSON em ``configs``). Uma linha por proposta permite transição atômica de status
(``UPDATE ... WHERE status = <esperado>``) e histórico completo, sem corte.

Migração puramente aditiva: cria a tabela e seus índices; nenhum dado existente
é alterado. Idempotente via inspect. O downgrade remove apenas o que este
upgrade criou.

Heads paralelos: outras branches também partem de ``r0s1t2u3v4w5`` (dedup
``s1t2u3v4w5x6`` e planka-groups ``w1s2c3r4e5m6``). Ao integrá-las, o Alembic
terá múltiplos heads; o merge (``alembic merge heads``) será feito na
integração. Esta migração não depende das outras nem toca suas tabelas, então a
ordem de aplicação entre elas é indiferente.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "m1p2r3o4p5s6"
down_revision: Union[str, None] = "r0s1t2u3v4w5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "project_merge_proposals"
_INDEXES = {
    "ix_project_merge_proposals_status": ["status"],
    "ix_project_merge_proposals_canonical": ["canonical"],
    "ix_project_merge_proposals_created_at": ["created_at"],
}


def upgrade() -> None:
    bind = op.get_bind()
    if _TABLE not in set(sa.inspect(bind).get_table_names()):
        op.create_table(
            _TABLE,
            sa.Column("id", sa.UUID(), primary_key=True),
            sa.Column("canonical", sa.String(), nullable=False),
            sa.Column("aliases", sa.JSON(), nullable=False),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
            sa.Column("origin", sa.String(length=32), nullable=True),
            sa.Column("source_job_id", sa.String(), nullable=True),
            sa.Column("apply_job_id", sa.String(), nullable=True),
            sa.Column("memory_counts", sa.JSON(), nullable=True),
            sa.Column("undo_info", sa.JSON(), nullable=True),
            sa.Column("decided_by", sa.String(), nullable=True),
            sa.Column("decided_at", sa.DateTime(), nullable=True),
            sa.Column("decision_note", sa.Text(), nullable=True),
            sa.Column("applied_at", sa.DateTime(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
    existing = {idx["name"] for idx in sa.inspect(bind).get_indexes(_TABLE)}
    for name, cols in _INDEXES.items():
        if name not in existing:
            op.create_index(name, _TABLE, cols)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    existing = {idx["name"] for idx in inspector.get_indexes(_TABLE)}
    for name in _INDEXES:
        if name in existing:
            op.drop_index(name, table_name=_TABLE)
    op.drop_table(_TABLE)
