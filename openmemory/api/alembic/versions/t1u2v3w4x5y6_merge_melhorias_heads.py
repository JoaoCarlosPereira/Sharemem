"""merge melhorias heads

Revision ID: t1u2v3w4x5y6
Revises: m1p2r3o4p5s6, s1t2u3v4w5x6, w1s2c3r4e5m6
Create Date: 2026-10-05 11:00:00.000000

Revision de merge (sem DDL) que une os três heads criados em paralelo sobre
``r0s1t2u3v4w5`` pelas branches do workspace "Melhorias do Mem0":

- ``m1p2r3o4p5s6`` — project_merge_proposals (feat/melhorias-mem0-merge-fix)
- ``s1t2u3v4w5x6`` — autodedup_reports (feat/melhorias-mem0-dedup)
- ``w1s2c3r4e5m6`` — spec_workspaces.created_by_email (feat/melhorias-mem0-planka-groups)

As três migrations são independentes (tabelas/colunas distintas), então a ordem
de aplicação entre elas é irrelevante.
"""
from typing import Sequence, Union

revision: str = "t1u2v3w4x5y6"
down_revision: Union[str, Sequence[str], None] = ("m1p2r3o4p5s6", "s1t2u3v4w5x6", "w1s2c3r4e5m6")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
