"""Auditoria de memorias que afirmam a mesma coisa (rodada 2, item 2).

    python -m app.scripts.audit_duplicate_groups [--threshold 0.88] [--project X]

Varre o acervo agrupando memorias semanticamente equivalentes e imprime os grupos
que importam:

* **estados divergentes** - alguem ja superou uma delas e as irmas continuam
  ativas. E exatamente o passivo que motivou esta auditoria: em 01/09/2026 a
  memoria 558075ce foi superada por ID e a bf2613bf, que afirmava a mesma coisa,
  ficou ativa e continuou voltando na busca como verdade.
* **todas ativas** - candidatas a consolidacao, ainda sem ninguem ter decidido.

Nao altera nada. E relatorio, e o insumo para calibrar
``MEM0_SUPERSEDE_SIBLING_SIMILARITY`` antes de ligar qualquer automatismo.
"""

from __future__ import annotations

import argparse
import sys


def _client():
    from app.mcp_server import get_memory_client_safe
    from app.utils.partitioning import bind_active_collection

    client = get_memory_client_safe()
    if client is None:
        raise SystemExit("cliente de memoria indisponivel")
    bind_active_collection(client)
    return client


def _all_points(client, project: str | None):
    """Todos os pontos, incluindo obsoletos - o interesse e justo a divergencia."""
    vs = client.vector_store
    scroll_filter = None
    if project:
        scroll_filter = {"must": [{"key": "project", "match": {"value": project}}]}
    offset = None
    while True:
        points, offset = vs.client.scroll(
            collection_name=vs.collection_name,
            scroll_filter=scroll_filter,
            limit=256,
            offset=offset,
            with_payload=True,
            with_vectors=True,
        )
        for p in points or []:
            yield p
        if offset is None:
            break


def _cosine(a, b) -> float:
    num = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return num / (na * nb) if na and nb else 0.0


def _vector_of(point):
    vec = getattr(point, "vector", None)
    if isinstance(vec, dict):
        # Colecao com vetores nomeados: usa o denso, unico com semantica aqui.
        for key in ("", "dense", "default"):
            if key in vec:
                return vec[key]
        return next(iter(vec.values()), None)
    return vec


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--threshold", type=float, default=0.88)
    parser.add_argument("--project", default=None)
    args = parser.parse_args()

    items = []
    for p in _all_points(_client(), args.project):
        vec = _vector_of(p)
        payload = getattr(p, "payload", {}) or {}
        if not vec:
            continue
        items.append(
            {
                "id": str(getattr(p, "id", "")),
                "vec": vec,
                "state": (payload.get("state") or "active").lower(),
                "text": payload.get("data") or "",
                "project": payload.get("project"),
                "created_at": payload.get("created_at"),
            }
        )

    print("pontos com vetor: %d" % len(items))

    # Uniao-busca simples sobre os pares acima do limiar. O acervo tem ordem de
    # 1e4 pontos; O(n^2) em memoria local e aceitavel para um relatorio avulso.
    parent = list(range(len(items)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if _cosine(items[i]["vec"], items[j]["vec"]) >= args.threshold:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[rj] = ri

    groups: dict[int, list[int]] = {}
    for i in range(len(items)):
        groups.setdefault(find(i), []).append(i)

    divergentes = []
    todas_ativas = []
    for members in groups.values():
        if len(members) < 2:
            continue
        estados = {items[i]["state"] for i in members}
        if len(estados) > 1:
            divergentes.append(members)
        elif estados == {"active"}:
            todas_ativas.append(members)

    def _dump(titulo, grupos):
        print()
        print("=== %s: %d grupos ===" % (titulo, len(grupos)))
        for members in grupos:
            print()
            for i in sorted(members, key=lambda k: items[k]["created_at"] or ""):
                it = items[i]
                print(
                    "  %-8s %s  [%s]  %s"
                    % (it["state"], it["id"], it["project"], (it["created_at"] or "")[:10])
                )
                print("      %s" % " ".join((it["text"] or "").split())[:140])

    _dump("ESTADOS DIVERGENTES (irmas ativas de algo ja superado)", divergentes)
    _dump("TODAS ATIVAS (candidatas a consolidacao)", todas_ativas)
    print()
    print("Nada foi alterado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
