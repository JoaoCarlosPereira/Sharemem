"""add spec_workspaces.created_by_email (pessoa criadora do workspace)

Revision ID: w1s2c3r4e5m6
Revises: r0s1t2u3v4w5
Create Date: 2026-10-02 15:30:00.000000

Aditiva: uma coluna anulável, sem índice (nenhuma consulta do código filtra por
ela) e sem backfill. ``created_by`` (hostname/ator) não muda. Idempotente via
inspect, como ``r0s1t2u3v4w5``.

MERGE: a branch ``feat/melhorias-mem0-dedup`` cria ``s1t2u3v4w5x6`` também sobre
``r0s1t2u3v4w5``. Ao juntar as duas, ajuste ``down_revision`` desta para
``s1t2u3v4w5x6`` (ou gere ``alembic merge``) para manter um único head.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "w1s2c3r4e5m6"
down_revision: Union[str, None] = "r0s1t2u3v4w5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "spec_workspaces"
_COLUMN = "created_by_email"


def _columns(inspector) -> set:
    return {c["name"] for c in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in set(inspector.get_table_names()):
        return
    if _COLUMN not in _columns(inspector):
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in set(inspector.get_table_names()):
        return
    if _COLUMN in _columns(inspector):
        with op.batch_alter_table(_TABLE) as batch:
            batch.drop_column(_COLUMN)
