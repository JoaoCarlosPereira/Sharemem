#!/usr/bin/env python3
"""Mede, SEM ESCREVER NADA, os scores de grupos de duplicatas já existentes no Qdrant.

O relatório do modo ``report`` (GET /admin/autodedup/report) só registra pares
vistos em escritas novas; grupos que já estão no Qdrant não aparecem lá. Este
script recebe os grupos-gabarito (JSON/YAML), embute cada membro com o MESMO
embedder e a MESMA chamada do autodedup e consulta o Qdrant — somente
``query_points``/``retrieve``. Saída: matriz intra/entre grupos e o intervalo de
limiar ``L`` com ``max_inter + margem <= L <= min_intra``.

Rode dentro do container do write-worker (mesma config/embedder). Na imagem a
API fica em ``/usr/src/openmemory`` e este script em
``/usr/src/openmemory/scripts`` (ver openmemory/api/Dockerfile); o script acha
sozinho o diretório com ``app/``, então funciona por caminho absoluto. ``-T``
evita TTY, senão stderr se mistura ao JSON redirecionado no host:

    docker compose -f docker-compose.scale.yml exec -T openmemory-write-worker \\
        python /usr/src/openmemory/scripts/autodedup-calibrate-groups.py \\
        /tmp/grupos.yaml --margin 0.01 > calibracao.json

``--validate-only`` só confere o arquivo de grupos (sem rede nem banco).

Embedder Ollama: o mem0 faria ``pull`` do modelo se ele não estivesse no
servidor Ollama. O script verifica antes com ``client.list()`` (só leitura) e
falha com mensagem clara se o modelo faltar — nunca baixa nada.

Formato do arquivo (``ids`` e/ou ``query``; ``top`` = hits da query usados):

    groups:
      - name: sicredi-trgn
        project: sysmovs
        query: "Sicredi 748 usa TRgnFinanceiroBoletoHibrido"
        top: 3
      - name: patch-204
        ids: ["<uuid1>", "<uuid2>"]

Nada é gravado: nem Qdrant, nem PostgreSQL/SQLite (só um SELECT em ``configs``),
nem ``autodedup_reports``. Ver docs/runbooks/autodedup-calibration.md.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path


def _bootstrap_sys_path(script: Path) -> None:
    """Põe no ``sys.path`` o diretório que contém o pacote ``app``.

    Rodar ``python <caminho>/autodedup-calibrate-groups.py`` coloca só a pasta do
    script em ``sys.path[0]`` (não o cwd), então ``import app`` falharia. Layouts:

    * imagem (openmemory/api/Dockerfile): API em ``/usr/src/openmemory`` (``app/``
      direto lá) e scripts em ``/usr/src/openmemory/scripts`` → ``parents[1]``;
    * checkout do repo: ``openmemory/scripts`` e ``openmemory/api/app`` →
      ``parents[1] / "api"``; o fork ``mem0`` fica na raiz (``parents[2]``) — na
      imagem ele já vem por ``PYTHONPATH=/usr/src``.
    """
    root = script.resolve().parents[1]
    for d in (root / "api", root):
        if (d / "app").is_dir():
            if str(d) not in sys.path:
                sys.path.insert(0, str(d))
            break
    repo_root = root.parent
    if (repo_root / "mem0").is_dir() and str(repo_root) not in sys.path:
        sys.path.append(str(repo_root))


_bootstrap_sys_path(Path(__file__))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("groups_file", help="JSON ou YAML com os grupos-gabarito")
    parser.add_argument("--margin", type=float, default=0.01, help="Folga mínima acima do max entre grupos")
    parser.add_argument("--top-k", type=int, default=None, help="Vizinhos por membro (padrão: MEM0_AUTODEDUP_TOP_K)")
    parser.add_argument("--compact", action="store_true", help="JSON sem indentação")
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Só valida o arquivo de grupos (sem Qdrant, sem embedder, sem banco) e sai",
    )
    args = parser.parse_args(argv)

    from app.utils.autodedup import autodedup_threshold, autodedup_top_k
    from app.utils.autodedup_calibration import (
        build_backends,
        calibrate,
        effective_memory_config,
        load_groups,
    )

    groups = load_groups(args.groups_file)
    if args.validate_only:
        summary = [{"name": g.name, "ids": len(g.ids), "query": bool(g.query), "project": g.project} for g in groups]
        json.dump({"groups": summary}, sys.stdout, ensure_ascii=False, indent=None if args.compact else 2)
        sys.stdout.write("\n")
        return 0
    # A montagem da config usa print() (app.utils.memory): manda para stderr
    # para que stdout seja só o JSON.
    with contextlib.redirect_stdout(sys.stderr):
        index, embed = build_backends(effective_memory_config())
        out = calibrate(
            groups,
            index,
            embed,
            margin=args.margin,
            top_k=args.top_k or autodedup_top_k(),
            current_threshold=autodedup_threshold(),
        )
    json.dump(out, sys.stdout, ensure_ascii=False, indent=None if args.compact else 2)
    sys.stdout.write("\n")
    for w in out["warnings"]:
        print(f"AVISO: {w}", file=sys.stderr)
    cal = out["calibration"]
    print(
        f"min_intra={cal['min_intra']} max_inter={cal['max_inter']} "
        f"intervalo={cal['interval']} recomendado={cal['recommended']}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
