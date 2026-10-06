"""add task archive fields (archived_at, archived_by)

Revision ID: r0s1t2u3v4w5
Revises: q9r0s1t2u3v4
Create Date: 2026-10-02 00:00:00.000000

Arquivamento não destrutivo de cards do Kanban (alternativa ao ``delete_task``).
Migração puramente aditiva: duas colunas anuláveis em ``task_cards`` e um índice
em ``archived_at``; nenhum dado existente é alterado (todo card atual continua
ativo, com ``archived_at`` nulo). Idempotente via inspect, mesma convenção de
``n9c0d1e2f3g4``. O downgrade remove apenas o que este upgrade criou.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r0s1t2u3v4w5"
down_revision: Union[str, None] = "q9r0s1t2u3v4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX = "ix_task_cards_archived_at"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "task_cards" not in set(inspector.get_table_names()):
        return

    cols = {c["name"] for c in inspector.get_columns("task_cards")}
    if "archived_at" not in cols:
        op.add_column("task_cards", sa.Column("archived_at", sa.DateTime(), nullable=True))
    if "archived_by" not in cols:
        op.add_column("task_cards", sa.Column("archived_by", sa.String(), nullable=True))

    indexes = {idx["name"] for idx in sa.inspect(bind).get_indexes("task_cards")}
    if _INDEX not in indexes:
        op.create_index(_INDEX, "task_cards", ["archived_at"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "task_cards" not in set(inspector.get_table_names()):
        return

    indexes = {idx["name"] for idx in inspector.get_indexes("task_cards")}
    if _INDEX in indexes:
        op.drop_index(_INDEX, table_name="task_cards")
    cols = {c["name"] for c in inspector.get_columns("task_cards")}
    with op.batch_alter_table("task_cards") as batch:
        if "archived_by" in cols:
            batch.drop_column("archived_by")
        if "archived_at" in cols:
            batch.drop_column("archived_at")
