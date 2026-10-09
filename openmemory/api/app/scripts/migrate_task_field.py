"""Migracao do campo `task` (problema 3) - DRY-RUN por padrao, NAO aplicado.

    python -m app.scripts.migrate_task_field                 # so relatorio
    python -m app.scripts.migrate_task_field --map map.json  # relatorio + destino
    python -m app.scripts.migrate_task_field --map map.json --apply

Hoje `project` acumula dois papeis incompativeis: repositorio/produto
(`sysmo-api-tributacao`, `sysmo-s1`, `sysmovs`) e numero de tarefa (`374954`,
`376140`, `373963`). Levantado em 01/09/2026: 10 projetos sao codigo de tarefa
puro, com 358 memorias; outros 6 usam o prefixo `tarefa-NNNNNN` e estao vazios.
Como as memorias de uma mesma tarefa tambem caem em `sysmo-s1` e
`sysmo-api-tributacao`, nao existe hoje forma confiavel de pedir "tudo da 371145".

A migracao faz duas coisas:

1. Backfill de `task` em TODA memoria cujo texto cite `TAREFA #NNNNNN` ou cujo
   `project` seja um codigo de tarefa. O campo `task` JA EXISTE no write-path e
   na busca (`app.utils.scope_keys`, `search_memory(task=...)`), indexado como
   keyword no Qdrant; o que falta e preencher o acervo anterior a ele.
2. Reatribuicao de `project` para as memorias cujo `project` e um codigo, usando
   o mapa `{"374954": "sysmo-s1", ...}` fornecido em `--map`. Sem mapa, `project`
   fica como esta e so o `task` e gravado - a migracao e incremental e reversivel.

NADA e escrito sem `--apply`. Sem `--apply` o script imprime o plano e sai 0.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys

TASK_CODE = re.compile(r"(?<!\d)#?(\d{6})(?!\d)")
PURE_TASK_PROJECT = re.compile(r"^(?:tarefa-)?(\d{6})$")


def task_of(project: str, text: str):
    """Chave de tarefa de uma memoria: o project quando e codigo, senao o texto.

    No texto so aceita a forma explicita `TAREFA #NNNNNN` / `tarefa NNNNNN` - um
    numero de 6 digitos solto no meio de uma frase nao e chave de nada.
    """
    m = PURE_TASK_PROJECT.match(project or "")
    if m:
        return m.group(1)
    m = re.search(r"(?i)tarefas?\s*#?\s*(\d{6})(?!\d)", text or "")
    return m.group(1) if m else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://192.168.3.213:8765")
    parser.add_argument("--map", dest="map_path", help="JSON {codigo: projeto_destino}")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="grava de fato; sem isso o script so imprime o plano",
    )
    args = parser.parse_args()

    from app.scripts.audit_escape_damage import iter_memories

    mapping = {}
    if args.map_path:
        with open(args.map_path, encoding="utf-8") as fh:
            mapping = json.load(fh)

    backfill = []       # (id, project, task) - so grava task
    reassign = []       # (id, project_atual, project_novo, task)
    sem_destino = collections.Counter()

    for project, item in iter_memories(args.base):
        text = item.get("content") or item.get("text") or ""
        task = task_of(project, text)
        if not task:
            continue
        if PURE_TASK_PROJECT.match(project or ""):
            destino = mapping.get(task)
            if destino:
                reassign.append((item["id"], project, destino, task))
            else:
                sem_destino[project] += 1
                backfill.append((item["id"], project, task))
        else:
            backfill.append((item["id"], project, task))

    print("PLANO DE MIGRACAO DO CAMPO `task`")
    print("  so backfill de task ......... %d memorias" % len(backfill))
    print("  backfill + troca de project . %d memorias" % len(reassign))
    if sem_destino:
        print()
        print("  projetos que SAO codigo de tarefa e nao tem destino no --map:")
        for proj, n in sem_destino.most_common():
            print("    %-12s %4d memorias" % (proj, n))
        print("  (essas ficam com o project atual; so o task e gravado)")

    if not args.apply:
        print()
        print("DRY-RUN: nada foi gravado. Rode com --apply para aplicar.")
        return 0

    raise SystemExit(
        "Aplicacao ainda nao habilitada: o passo de escrita depende do endpoint "
        "de atualizacao de payload em massa, que so deve ser ligado depois da "
        "aprovacao do plano e de um backup (`python -m app.scripts.run_backup`)."
    )


if __name__ == "__main__":
    sys.exit(main())
