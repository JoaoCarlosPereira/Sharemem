#!/usr/bin/env python3
"""Mede p50/p95 do caminho de busca SEM e COM rerank (card 80110071).

SOMENTE LEITURA: embeda a consulta, busca o pool de candidatos no Qdrant
(MEM0_SEARCH_CANDIDATE_K) e cronometra cada etapa. Nao chama ``search_memory``
(evita cadastro de usuario/auditoria de leitura), nao usa o read cache (cada
consulta e embedada de novo) e nao grava nada no Qdrant nem no PostgreSQL.

Rode DENTRO do container da API, como na medicao do PR #28. Na imagem a API fica
em ``/usr/src/openmemory`` e este script em ``/usr/src/openmemory/scripts`` (ver
openmemory/api/Dockerfile); o script acha sozinho o diretorio com ``app/``, entao
funciona por caminho absoluto. ``-T`` evita TTY (necessario ao redirecionar a
saida, senao stderr se mistura ao arquivo no host):

    docker compose -f docker-compose.scale.yml exec -T openmemory-mcp \\
        python /usr/src/openmemory/scripts/bench-rerank-latency.py --rounds 3

``--check-imports`` so confere que os modulos do app resolvem (sem rede, Qdrant,
banco nem torch). ``--help`` nao importa nada do app.

Consultas: as padrao abaixo (distintas, PT-BR) ou ``--queries arquivo.txt`` (uma
por linha). Para comparar modelos/TOP_N sem recriar o container:

    ... exec -T -e MEM0_RERANKER_PROVIDER=sentence_transformer \\
            -e MEM0_RERANKER_MODEL=cross-encoder/mmarco-mMiniLMv2-L12-H384-v1 \\
            -e MEM0_RERANKER_TOP_N=30 openmemory-mcp python .../bench-rerank-latency.py

Sem MEM0_RERANKER_PROVIDER mede apenas o "antes" (baseline).

Boost de grupo: por padrao o bench ranqueia SEM grupo do solicitante (o boost de
grupo vale 1.0 para todos). ``--group NOME`` usa esse grupo; ``--owner HOSTNAME``
resolve o grupo do hostname com ``group_of_hostname`` (somente leitura; NAO usa
``requester_group_for_mcp``, que cadastra o usuario).

Memoria: com o provider ligado o script carrega a SUA copia do modelo, alem da que
o processo uvicorn ja tem — pico de RAM ~ +1 modelo (centenas de MB) enquanto roda.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path


def _bootstrap_sys_path(script: Path) -> None:
    """Poe no ``sys.path`` o diretorio que contem o pacote ``app``.

    Rodar ``python <caminho>/bench-rerank-latency.py`` coloca so a pasta do script
    em ``sys.path[0]`` (nao o cwd), entao ``import app`` falharia. Layouts:

    * imagem (openmemory/api/Dockerfile): API em ``/usr/src/openmemory`` (``app/``
      direto la) e scripts em ``/usr/src/openmemory/scripts`` -> ``parents[1]``;
    * checkout do repo: ``openmemory/scripts`` e ``openmemory/api/app`` ->
      ``parents[1] / "api"``; o fork ``mem0`` fica na raiz (``parents[2]``) — na
      imagem ele ja vem por ``PYTHONPATH=/usr/src``.
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

DEFAULT_QUERIES = [
    "como evitar perder o volume do qdrant",
    "backup periodico no minio",
    "reenfileirar jobs da write_queue",
    "boost de recencia na busca",
    "token de admin para rotas destrutivas",
    "login google device flow",
    "familias de projeto no ranking",
    "timeout do embedding no ollama",
    "isolamento de grupo no kanban",
    "dedup semantica depois da escrita",
    "configuracao do pgbouncer",
    "arquivamento de cards concluidos",
]


def _pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, round(p / 100 * (len(ordered) - 1))))
    return ordered[k]


def _summary(values: list[float]) -> dict:
    return {
        "n": len(values),
        "p50_ms": round(statistics.median(values), 1) if values else None,
        "p95_ms": round(_pct(values, 95), 1) if values else None,
        "max_ms": round(max(values), 1) if values else None,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--queries", help="arquivo com uma consulta por linha")
    parser.add_argument("--rounds", type=int, default=1, help="repeticoes de cada consulta (o embed e refeito)")
    parser.add_argument("--project", default="mem0-shared", help="projeto preferido no ranking")
    parser.add_argument("--group", help="grupo do solicitante para o boost de grupo")
    parser.add_argument("--owner", help="hostname do solicitante; o grupo e resolvido (somente leitura)")
    parser.add_argument("--json", action="store_true", help="saida JSON")
    parser.add_argument(
        "--check-imports",
        action="store_true",
        help="so confere os imports do app e sai (sem rede, Qdrant, banco nem torch)",
    )
    args = parser.parse_args(argv)

    queries = DEFAULT_QUERIES
    if args.queries:
        with open(args.queries, encoding="utf-8") as fh:
            queries = [ln.strip() for ln in fh if ln.strip()]

    # Imports do app so depois do parse: ``--help`` nao carrega nada. Nenhum destes
    # importa torch (o reranker so e construido em get_reranker()).
    from app.mcp_server import DEFAULT_SEARCH_CANDIDATE_K, DEFAULT_SEARCH_TOP_K
    from app.utils import reranking
    from app.utils.attribution import author_hostname_from_payload
    from app.utils.memory import get_memory_client_safe
    from app.utils.partitioning import bind_active_collection
    from app.utils.recency import rank_search_results

    if args.check_imports:
        print(
            json.dumps(
                {
                    "imports": "ok",
                    "candidate_k": DEFAULT_SEARCH_CANDIDATE_K,
                    "torch_loaded": "torch" in sys.modules,
                }
            )
        )
        return 0

    client = get_memory_client_safe()
    if client is None:
        print("ERRO: memory client indisponivel", file=sys.stderr)
        return 2
    bind_active_collection(client)

    requester_group = args.group
    if not requester_group and args.owner:
        from app.utils.groups import group_of_hostname

        requester_group = group_of_hostname(args.owner)
        if requester_group is None:
            print(f"AVISO: hostname {args.owner!r} sem grupo; boost de grupo desligado", file=sys.stderr)

    rerank_on = bool(reranking.reranker_provider())
    if rerank_on:
        t0 = time.perf_counter()
        instance, err = reranking.get_reranker()
        load_ms = (time.perf_counter() - t0) * 1000
        if instance is None:
            print(f"ERRO: reranker indisponivel: {err}", file=sys.stderr)
            return 2
        reranking._warm(instance)  # primeira inferencia fora da medicao

    stages: dict[str, list[float]] = {"embed": [], "qdrant": [], "rank": [], "rerank": [], "total_sem": [], "total_com": []}
    outcomes: dict[str, int] = {}
    for _ in range(max(1, args.rounds)):
        for q in queries:
            t = time.perf_counter()
            vec = client.embedding_model.embed(q, "search")
            embed_ms = (time.perf_counter() - t) * 1000

            t = time.perf_counter()
            hits = client.vector_store.search(query=q, vectors=vec, top_k=DEFAULT_SEARCH_CANDIDATE_K, filters=None)
            qdrant_ms = (time.perf_counter() - t) * 1000
            pool = [
                {
                    "id": str(h.id),
                    "memory": (h.payload or {}).get("data"),
                    "project": (h.payload or {}).get("project"),
                    "owner": author_hostname_from_payload(h.payload or {}),
                    "created_at": (h.payload or {}).get("created_at"),
                    "updated_at": (h.payload or {}).get("updated_at"),
                    "score": h.score,
                }
                for h in hits
            ]

            t = time.perf_counter()
            rank_search_results(
                pool, preferred_project=args.project, requester_group=requester_group, annotate=True, query=q
            )
            rank_ms = (time.perf_counter() - t) * 1000

            stages["embed"].append(embed_ms)
            stages["qdrant"].append(qdrant_ms)
            stages["rank"].append(rank_ms)
            stages["total_sem"].append(embed_ms + qdrant_ms + rank_ms)

            if rerank_on:
                t = time.perf_counter()
                status = reranking.apply_rerank(q, pool, page_size=DEFAULT_SEARCH_TOP_K)
                if status.get("applied"):
                    head = pool[: status["reranked"]]
                    rank_search_results(
                        head, preferred_project=args.project, requester_group=requester_group, annotate=True, query=q
                    )
                rerank_ms = (time.perf_counter() - t) * 1000
                key = "applied" if status.get("applied") else str(status.get("reason")).split(":")[0]
                outcomes[key] = outcomes.get(key, 0) + 1
                stages["rerank"].append(rerank_ms)
                stages["total_com"].append(embed_ms + qdrant_ms + rank_ms + rerank_ms)

    report = {
        "candidate_k": DEFAULT_SEARCH_CANDIDATE_K,
        "queries": len(queries),
        "rounds": args.rounds,
        "requester_group": requester_group,
        "rerank": (
            {
                "provider": reranking.reranker_provider(),
                "model": reranking.reranker_model(),
                "top_n": reranking.rerank_top_n(DEFAULT_SEARCH_TOP_K),
                "timeout_sec": reranking.rerank_timeout_seconds(),
                "load_ms": round(load_ms, 1),
                "outcomes": outcomes,
                "threads": reranking.reranker_threads(),
                "offline": not reranking.downloads_allowed(),
            }
            if rerank_on
            else None
        ),
        "stages": {k: _summary(v) for k, v in stages.items() if v},
    }
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(
            f"candidate_k={report['candidate_k']} consultas={report['queries']} rounds={report['rounds']} "
            f"grupo={report['requester_group'] or '-'}"
        )
        if report["rerank"]:
            print("rerank:", json.dumps(report["rerank"]))
        print(f"{'etapa':<10} {'n':>4} {'p50 ms':>9} {'p95 ms':>9} {'max ms':>9}")
        for name, s in report["stages"].items():
            print(f"{name:<10} {s['n']:>4} {s['p50_ms']:>9} {s['p95_ms']:>9} {s['max_ms']:>9}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
