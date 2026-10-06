#!/usr/bin/env python3
"""Auditoria SOMENTE LEITURA do isolamento de Kanban por grupo (card 2feaabe4).

Lista o que ficaria invisível/ambíguo após o deploy de "Mostrar os quadros
kanban apenas do grupo do usuario":

* workspaces com ``group_id`` NULL (fail-closed: board some para TODOS);
* workspaces sem ``created_by_email`` (criados antes da coluna ou por máquina
  sem pessoa vinculada) — só informativo;
* boards PLANKA não mapeados em ``spec_planka_id_map`` (resolvidos pelo grupo
  do criador do board; criados por ator técnico ficam invisíveis);
* projetos mapeados em ``spec_planka_id_map`` sem ``project_manager`` do
  DEFAULT_ADMIN (``PLANKA_DEFAULT_ADMIN_EMAIL``/``DEFAULT_ADMIN_EMAIL``, padrão
  ``admin@mem0.local``): o espelho não consegue arquivar/concluir (404).

NÃO existe modo de escrita: o script só faz SELECT, e no PostgreSQL abre a
transação como ``READ ONLY`` (qualquer escrita acidental falharia). Corrigir
dados é decisão humana, fora deste script.

Uso (dentro do container da API, NUNCA automatizado contra produção sem pedido):

    docker compose -f docker-compose.scale.yml exec openmemory-mcp \\
        python /usr/src/openmemory/scripts/audit-kanban-group-isolation.py [--json]

``MEM0_PG_SCHEMA`` (padrão ``planka``) indica o schema das tabelas do PLANKA.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Ator técnico do espelho (DEFAULT_ADMIN_EMAIL do serviço planka no compose).
DEFAULT_ADMIN_EMAIL = "admin@mem0.local"


def _rows(db, sql: str, **params) -> list[dict]:
    from sqlalchemy import text

    return [dict(r._mapping) for r in db.execute(text(sql), params)]


def collect(db, planka_schema: str, admin_email: str = DEFAULT_ADMIN_EMAIL) -> dict:
    is_pg = db.bind.dialect.name == "postgresql"
    if is_pg:
        db.execute(__import__("sqlalchemy").text("SET TRANSACTION READ ONLY"))

    report: dict = {
        "workspaces_without_group": _rows(
            db,
            "SELECT id, project_id, slug, created_by, created_at FROM spec_workspaces "
            "WHERE group_id IS NULL ORDER BY created_at",
        ),
        "workspaces_without_creator_email": len(
            _rows(db, "SELECT id FROM spec_workspaces WHERE created_by_email IS NULL")
        ),
        "unmapped_planka_boards": None,
        "mapped_projects_without_admin_manager": None,
    }

    if is_pg and _SCHEMA_RE.match(planka_schema):
        report["unmapped_planka_boards"] = _rows(
            db,
            f'SELECT b.id::text AS board_id, b.project_id::text AS project_id, '
            f'b.name AS board_name, u.email AS creator_email '
            f'FROM "{planka_schema}".board AS b '
            f'LEFT JOIN "{planka_schema}".user_account AS u ON u.id = b.creator_user_id '
            f"WHERE NOT EXISTS (SELECT 1 FROM public.spec_planka_id_map m "
            f"WHERE m.entity_type = 'board' AND m.planka_id = b.id::text) "
            f"ORDER BY b.created_at",
        )
        # Sem gerência, o PATCH /api/projects/:id do espelho (set_project_lifecycle)
        # cai em 404: arquivado/concluído fica divergente do Spec.
        report["mapped_projects_without_admin_manager"] = _rows(
            db,
            f"SELECT m.planka_id AS project_id, m.spec_id::text AS workspace_id, "
            f"p.name AS project_name "
            f"FROM public.spec_planka_id_map AS m "
            f'JOIN "{planka_schema}".project AS p ON p.id::text = m.planka_id '
            f"WHERE m.entity_type = 'project' "
            f"AND NOT EXISTS (SELECT 1 FROM \"{planka_schema}\".project_manager AS pm "
            f'JOIN "{planka_schema}".user_account AS u ON u.id = pm.user_id '
            f"WHERE pm.project_id = p.id AND lower(u.email) = lower(:admin_email)) "
            f"ORDER BY p.created_at",
            admin_email=admin_email,
        )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Saída JSON")
    args = parser.parse_args()

    from app.database import SessionLocal

    db = SessionLocal()
    try:
        admin_email = (
            os.getenv("PLANKA_DEFAULT_ADMIN_EMAIL")
            or os.getenv("DEFAULT_ADMIN_EMAIL")
            or DEFAULT_ADMIN_EMAIL
        ).strip()
        report = collect(
            db,
            os.getenv("MEM0_PG_SCHEMA", "planka").strip() or "planka",
            admin_email,
        )
    finally:
        db.rollback()  # nada a confirmar: leitura apenas
        db.close()

    if args.json:
        print(json.dumps(report, default=str, indent=2))
        return 0

    no_group = report["workspaces_without_group"]
    print(f"Workspaces com group_id NULL (invisíveis para todos): {len(no_group)}")
    for row in no_group:
        print(f"  - {row['project_id']}/{row['slug']} criado_por={row['created_by']}")
    print(f"Workspaces sem created_by_email: {report['workspaces_without_creator_email']}")
    unmapped = report["unmapped_planka_boards"]
    if unmapped is None:
        print("Boards PLANKA não mapeados: n/d (requer PostgreSQL com schema do PLANKA)")
    else:
        print(f"Boards PLANKA não mapeados (grupo pelo criador do board): {len(unmapped)}")
        for row in unmapped:
            print(f"  - board={row['board_id']} criador={row['creator_email'] or '?'}")
    no_admin = report["mapped_projects_without_admin_manager"]
    if no_admin is None:
        print("Projetos mapeados sem gerência do DEFAULT_ADMIN: n/d (requer PostgreSQL)")
    else:
        print(
            "Projetos mapeados sem gerência do DEFAULT_ADMIN "
            f"(arquivar/concluir pelo espelho falha com 404): {len(no_admin)}"
        )
        for row in no_admin:
            print(f"  - project={row['project_id']} workspace={row['workspace_id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
