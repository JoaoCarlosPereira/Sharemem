"""Calibração do ``MEM0_AUTODEDUP_THRESHOLD`` com grupos de duplicatas JÁ existentes.

O relatório do modo ``report`` (``autodedup_reports``) só vê pares no momento de
uma escrita nova: grupos de duplicatas que já estão no Qdrant (o gabarito do card
ddbc19a9 — Sicredi/TRgn, PATCH 204, Fin104, Fcr722) nunca aparecem lá. Este
módulo mede o score desses pares **sem escrever nada**:

* o texto de cada membro é embutido pelo MESMO embedder e com a MESMA chamada do
  autodedup (``embed(texto, "search")``) e comparado aos vetores armazenados no
  Qdrant (``query_points``) — o mesmo cosseno que ``find_near_duplicates`` veria
  se aquele membro fosse a memória nova;
* para cada par (a, b) há dois sentidos (a como nova x b armazenada e vice-versa).
  Intra-grupo usa o **menor** dos dois (o pior caso para pegar a duplicata);
  entre grupos usa o **maior** (o pior caso de falso positivo);
* a saída traz a matriz de scores, ``min_intra``, ``max_inter`` e o intervalo de
  limiar ``L`` que separa os dois (``max_inter < L <= min_intra``), com margem.

Somente leitura por construção: :class:`ReadOnlyVectorIndex` expõe apenas
``query_points``/``retrieve`` do cliente Qdrant; nada aqui chama
``add``/``update``/``delete``/``upsert``/``set_payload``, abre sessão SQL de
escrita ou grava em ``autodedup_reports``. O ponto de entrada é
``openmemory/scripts/autodedup-calibrate-groups.py``.
"""

from __future__ import annotations

import itertools
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

DEFAULT_MARGIN = 0.01
DEFAULT_TOP_K = 10


# --------------------------------------------------------------------------- #
# Entrada
# --------------------------------------------------------------------------- #
@dataclass
class GroupSpec:
    """Um grupo de memórias que afirmam a MESMA coisa (gabarito).

    Membros vêm de ``ids`` explícitos e/ou de ``query``: a query é embutida e os
    ``top`` primeiros hits (no ``project``, se dado) viram membros. A saída
    sempre lista id + texto de cada membro para conferência humana — uma query
    pode trazer memória errada, e então o grupo deve ser fixado por ``ids``.
    """

    name: str
    ids: list[str] = field(default_factory=list)
    query: Optional[str] = None
    project: Optional[str] = None
    top: int = 3


def load_groups(path: str | Path) -> list[GroupSpec]:
    """Lê ``{"groups": [...]}`` (ou a lista direto) de um arquivo JSON ou YAML."""
    p = Path(path)
    raw_text = p.read_text(encoding="utf-8")
    if p.suffix.lower() in (".yaml", ".yml"):
        import yaml

        data = yaml.safe_load(raw_text)
    else:
        data = json.loads(raw_text)
    return parse_groups(data)


def parse_groups(data: Any) -> list[GroupSpec]:
    items = data.get("groups") if isinstance(data, dict) else data
    if not isinstance(items, list) or not items:
        raise ValueError("arquivo de grupos vazio: esperado {'groups': [...]} ou uma lista")
    groups: list[GroupSpec] = []
    names: set[str] = set()
    for i, g in enumerate(items):
        if not isinstance(g, dict):
            raise ValueError(f"grupo #{i} não é um objeto")
        name = str(g.get("name") or f"grupo-{i + 1}")
        if name in names:
            raise ValueError(f"nome de grupo duplicado: {name}")
        names.add(name)
        ids = [str(x) for x in (g.get("ids") or [])]
        query = g.get("query")
        if not ids and not query:
            raise ValueError(f"grupo {name!r} precisa de 'ids' ou 'query'")
        top = int(g.get("top", 3))
        if top < 1:
            raise ValueError(f"grupo {name!r}: 'top' deve ser >= 1")
        groups.append(
            GroupSpec(name=name, ids=ids, query=query, project=g.get("project"), top=top)
        )
    return groups


# --------------------------------------------------------------------------- #
# Acesso SOMENTE LEITURA ao Qdrant
# --------------------------------------------------------------------------- #
@dataclass
class Point:
    id: str
    score: Optional[float]
    payload: dict


def _governance_filter(*, include_obsolete: bool):
    """Mesmo guarda de estado que ``mem0.vector_stores.qdrant.Qdrant.search`` aplica.

    Usa os métodos puros da classe sem instanciá-la: ``Qdrant.__init__`` chama
    ``create_col`` (cria coleção/índices) e não é somente leitura.
    """
    from mem0.vector_stores.qdrant import Qdrant

    builder = object.__new__(Qdrant)
    return builder._create_filter(
        builder._merge_governance_filters(None, include_obsolete=include_obsolete)
    )


class ReadOnlyVectorIndex:
    """Fachada mínima sobre ``QdrantClient``: só ``query_points`` e ``retrieve``."""

    def __init__(self, client, collection_name: str):
        self._client = client
        self._collection = collection_name

    def _query(self, vector, *, limit: int, must: Sequence = (), include_obsolete: bool) -> list[Point]:
        from qdrant_client import models

        guard = _governance_filter(include_obsolete=include_obsolete)
        conditions = [*must, guard] if guard is not None else list(must)
        res = self._client.query_points(
            collection_name=self._collection,
            query=vector,
            query_filter=models.Filter(must=conditions) if conditions else None,
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        return [
            Point(id=str(h.id), score=float(h.score), payload=dict(h.payload or {}))
            for h in (getattr(res, "points", res) or [])
        ]

    def search(self, vector, *, top_k: int) -> list[Point]:
        """Exatamente a busca do autodedup: top_k, sem filtro além do guarda ativo."""
        return self._query(vector, limit=top_k, include_obsolete=False)

    def search_project(self, vector, *, project: Optional[str], top_k: int) -> list[Point]:
        from qdrant_client import models

        must = []
        if project:
            must.append(models.FieldCondition(key="project", match=models.MatchValue(value=project)))
        return self._query(vector, limit=top_k, must=must, include_obsolete=False)

    def score_against(self, vector, ids: Sequence[str]) -> dict[str, float]:
        """Cosseno do ``vector`` contra cada ponto de ``ids`` (busca restrita por id).

        Inclui obsoletas: um membro do gabarito já supersedido manualmente ainda
        serve para medir o score. Quarentenadas continuam ocultas.
        """
        from qdrant_client import models

        if not ids:
            return {}
        hits = self._query(
            vector,
            limit=len(ids),
            must=[models.HasIdCondition(has_id=list(ids))],
            include_obsolete=True,
        )
        return {h.id: h.score for h in hits}

    def retrieve(self, ids: Sequence[str]) -> dict[str, Point]:
        if not ids:
            return {}
        recs = self._client.retrieve(
            collection_name=self._collection, ids=list(ids), with_payload=True, with_vectors=False
        )
        return {str(r.id): Point(id=str(r.id), score=None, payload=dict(r.payload or {})) for r in recs}


# --------------------------------------------------------------------------- #
# Cálculo
# --------------------------------------------------------------------------- #
@dataclass
class Member:
    id: str
    group: str
    text: str
    project: Optional[str]
    state: Optional[str]


def resolve_members(
    groups: Iterable[GroupSpec], index: ReadOnlyVectorIndex, embed: Callable[[str], list]
) -> tuple[list[Member], list[str]]:
    """Membros de cada grupo (ids explícitos + hits da query). Retorna (membros, avisos)."""
    members: list[Member] = []
    warnings: list[str] = []
    owner: dict[str, str] = {}
    for g in groups:
        ids = list(dict.fromkeys(g.ids))
        if g.query:
            for h in index.search_project(embed(g.query), project=g.project, top_k=g.top):
                if h.id not in ids:
                    ids.append(h.id)
        found = index.retrieve(ids)
        for mid in ids:
            p = found.get(mid)
            if p is None:
                warnings.append(f"{g.name}: memória {mid} não encontrada no Qdrant (ignorada)")
                continue
            if mid in owner:
                warnings.append(f"{g.name}: memória {mid} já pertence a {owner[mid]} (ignorada)")
                continue
            if (p.payload.get("state") or "").lower() == "quarantined":
                # A busca (do autodedup e daqui) nunca vê quarentenadas.
                warnings.append(f"{g.name}: memória {mid} quarentenada (ignorada)")
                continue
            text = (p.payload.get("data") or "").strip()
            if not text:
                warnings.append(f"{g.name}: memória {mid} sem texto (ignorada)")
                continue
            owner[mid] = g.name
            members.append(
                Member(
                    id=mid,
                    group=g.name,
                    text=text,
                    project=p.payload.get("project"),
                    state=p.payload.get("state"),
                )
            )
        if sum(1 for m in members if m.group == g.name) < 2:
            warnings.append(f"{g.name}: menos de 2 membros válidos — não gera par intra-grupo")
    return members, warnings


def _r(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(x, 4)


def calibrate(
    groups: Iterable[GroupSpec],
    index: ReadOnlyVectorIndex,
    embed: Callable[[str], list],
    *,
    margin: float = DEFAULT_MARGIN,
    top_k: int = DEFAULT_TOP_K,
    current_threshold: Optional[float] = None,
) -> dict:
    """Matriz de scores intra/entre grupos e o intervalo de limiar que os separa."""
    if not (math.isfinite(margin) and 0 <= margin < 0.5):
        raise ValueError("margin deve estar em [0, 0.5)")
    members, warnings = resolve_members(groups, index, embed)
    ids = [m.id for m in members]
    by_id = {m.id: m for m in members}

    # directed[a][b] = score com ``a`` como memória nova e ``b`` armazenada.
    directed: dict[str, dict[str, float]] = {}
    outside: dict[str, list[dict]] = {}
    for m in members:
        vec = embed(m.text)
        others = [i for i in ids if i != m.id]
        directed[m.id] = index.score_against(vec, others)
        # Vizinhos que o autodedup veria de fato (top_k) e que não estão em
        # nenhum grupo: não rotulados, mas o maior deles merece revisão manual.
        outside[m.id] = [
            {"id": h.id, "score": _r(h.score), "project": h.payload.get("project"),
             "text": (h.payload.get("data") or "")[:160]}
            for h in index.search(vec, top_k=top_k)
            if h.id not in by_id
        ]

    pairs = []
    for a, b in itertools.combinations(ids, 2):
        ab, ba = directed[a].get(b), directed[b].get(a)
        known = [s for s in (ab, ba) if s is not None]
        if not known:
            warnings.append(f"par {a} x {b}: sem score (ponto quarentenado?)")
            continue
        same = by_id[a].group == by_id[b].group
        pairs.append(
            {
                "a": a,
                "b": b,
                "group_a": by_id[a].group,
                "group_b": by_id[b].group,
                "kind": "intra" if same else "inter",
                "score_a_new": _r(ab),
                "score_b_new": _r(ba),
                # Intra: pior caso p/ capturar; entre: pior caso de falso positivo.
                "score": min(known) if same else max(known),
            }
        )

    intra = [p["score"] for p in pairs if p["kind"] == "intra"]
    inter = [p["score"] for p in pairs if p["kind"] == "inter"]
    min_intra = min(intra) if intra else None
    max_inter = max(inter) if inter else None
    calibration = threshold_interval(min_intra, max_inter, margin=margin)
    if current_threshold is not None:
        calibration["current_threshold"] = current_threshold
        calibration["current_ok"] = _threshold_ok(current_threshold, min_intra, max_inter, margin)

    for p in pairs:
        p["score"] = _r(p["score"])

    return {
        "members": [
            {"id": m.id, "group": m.group, "project": m.project, "state": m.state, "text": m.text[:160]}
            for m in members
        ],
        "groups": _group_matrix(members, pairs),
        "pairs": sorted(pairs, key=lambda p: (p["kind"], -(p["score"] or 0))),
        "outside_neighbors": {
            mid: rows for mid, rows in outside.items() if rows
        },
        "max_outside_neighbor": _r(
            max((r["score"] for rows in outside.values() for r in rows if r["score"] is not None),
                default=None)
        ),
        "calibration": calibration,
        "warnings": warnings,
    }


def _group_matrix(members: list[Member], pairs: list[dict]) -> dict:
    """Por grupo: min intra; por par de grupos: max entre (matriz grupo x grupo)."""
    names = list(dict.fromkeys(m.group for m in members))
    matrix: dict[str, dict[str, Optional[float]]] = {n: {o: None for o in names} for n in names}
    for p in pairs:
        ga, gb, s = p["group_a"], p["group_b"], p["score"]
        if ga == gb:
            cur = matrix[ga][ga]
            matrix[ga][ga] = s if cur is None else min(cur, s)
        else:
            for x, y in ((ga, gb), (gb, ga)):
                cur = matrix[x][y]
                matrix[x][y] = s if cur is None else max(cur, s)
    return {
        "order": names,
        "legend": "diagonal = menor score intra-grupo; fora da diagonal = maior score entre os grupos",
        "matrix": matrix,
    }


def _threshold_ok(t: float, min_intra, max_inter, margin: float) -> bool:
    if min_intra is not None and t > min_intra:
        return False
    if max_inter is not None and t < max_inter + margin:
        return False
    return True


def threshold_interval(
    min_intra: Optional[float], max_inter: Optional[float], *, margin: float = DEFAULT_MARGIN
) -> dict:
    """Intervalo de ``L`` com ``max_inter < L <= min_intra`` (``apply`` usa ``>=``).

    Com margem ``m``: ``L ∈ [max_inter + m, min_intra]`` — o falso positivo mais
    alto fica pelo menos ``m`` abaixo de L e todos os pares intra continuam
    ``>= L``. ``recommended`` é o topo do intervalo arredondado para baixo em
    0.01 (mais conservador contra falso positivo, ainda pegando o gabarito).
    """
    out: dict[str, Any] = {
        "min_intra": _r(min_intra),
        "max_inter": _r(max_inter),
        "margin": margin,
        "gap": None,
        "separable": None,
        "interval": None,
        "recommended": None,
    }
    if min_intra is None or max_inter is None:
        out["note"] = "é preciso ao menos um par intra-grupo e um par entre grupos"
        return out
    gap = min_intra - max_inter
    out["gap"] = _r(gap)
    out["separable"] = gap > 0
    lo, hi = max_inter + margin, min_intra
    if lo <= hi:
        out["interval"] = {"min": _r(lo), "max": _r(hi)}
        # round(..., 6) antes do floor: 0.57 * 100 == 56.99999999999999 em float.
        rec = math.floor(round(hi * 100, 6)) / 100
        out["recommended"] = round(rec if rec >= lo else hi, 4)
    else:
        out["note"] = (
            "sem limiar seguro: o maior par entre grupos está a menos de "
            f"{margin} do menor par intra-grupo — não use apply neste domínio"
        )
    return out


# --------------------------------------------------------------------------- #
# Montagem dos backends reais (usada só pelo script)
# --------------------------------------------------------------------------- #
def effective_memory_config() -> dict:
    """Config efetiva do cliente mem0, montada como ``get_memory_client`` faz.

    Só LÊ a linha ``configs.main`` (SELECT) — nenhuma escrita SQL.
    """
    from app.database import SessionLocal
    from app.models import Config as ConfigModel
    from app.utils.memory import (
        _fix_ollama_urls_if_localhost,
        _parse_environment_variables,
        get_default_memory_config,
        sanitize_mem0_config,
    )

    config = get_default_memory_config()
    mem0_cfg: dict = {}
    db = SessionLocal()
    try:
        row = db.query(ConfigModel).filter(ConfigModel.key == "main").first()
        mem0_cfg = ((row.value or {}).get("mem0") or {}) if row else {}
    except Exception as exc:  # noqa: BLE001 - sem tabela configs: usa os defaults
        print(f"AVISO: config do banco indisponível ({exc}); usando defaults do ambiente")
    finally:
        db.rollback()
        db.close()
    for key in ("llm", "embedder", "vector_store"):
        if mem0_cfg.get(key) is not None:
            config[key] = mem0_cfg[key]
    if (config.get("embedder") or {}).get("provider") == "ollama":
        config["embedder"] = _fix_ollama_urls_if_localhost(config["embedder"])
    return sanitize_mem0_config(_parse_environment_variables(config))


def _ollama_name(name: str) -> str:
    # Mesma normalização de mem0.embeddings.ollama.OllamaEmbedding.
    return name if ":" in name else f"{name}:latest"


def ensure_ollama_model_present(emb_config: dict, client_factory: Optional[Callable[..., Any]] = None) -> None:
    """Falha (RuntimeError) se o modelo de embedding não estiver no servidor Ollama.

    ``OllamaEmbedding.__init__`` chama ``_ensure_model_exists()``, que faz
    ``client.pull()`` quando o modelo falta — baixaria GBs e alteraria o servidor
    Ollama compartilhado a partir de um script que se diz somente leitura. Aqui
    só ``client.list()`` (leitura); se o modelo existir, o ``__init__`` do mem0
    também só lista e não puxa nada.
    """
    if client_factory is None:
        from ollama import Client as client_factory

    model = emb_config.get("model") or "nomic-embed-text"  # default do mem0
    client = client_factory(host=emb_config.get("ollama_base_url"))
    models_ = client.list()["models"]
    target = _ollama_name(model)
    names = {_ollama_name(m.get(k) or "") for m in models_ for k in ("name", "model")}
    if target not in names:
        raise RuntimeError(
            f"modelo de embedding Ollama '{model}' ausente em {emb_config.get('ollama_base_url') or 'padrão'}; "
            "o script não baixa modelos (o mem0 faria pull). Confira MEM0 embedder/config."
        )


def build_backends(config: dict) -> tuple[ReadOnlyVectorIndex, Callable[[str], list]]:
    """Embedder do autodedup + índice Qdrant somente leitura, a partir da config."""
    from qdrant_client import QdrantClient

    from app.utils.env import is_local_only
    from mem0.utils.factory import EmbedderFactory

    if is_local_only():
        from app.utils.memory import _local_only_violations

        violations = [v for v in _local_only_violations(config) if v.startswith("embedder")]
        if violations:
            raise RuntimeError(f"MEM0_LOCAL_ONLY: embedder não local ({', '.join(violations)})")

    vs = config.get("vector_store") or {}
    if (vs.get("provider") or "qdrant") != "qdrant":
        raise RuntimeError(f"só Qdrant é suportado (vector_store={vs.get('provider')})")
    vcfg = vs.get("config") or {}
    params: dict[str, Any] = {}
    if vcfg.get("url"):
        params["url"] = vcfg["url"]
    if vcfg.get("api_key"):
        params["api_key"] = vcfg["api_key"]
    if vcfg.get("host") and vcfg.get("port"):
        params["host"], params["port"] = vcfg["host"], vcfg["port"]
    if not params:
        raise RuntimeError("Qdrant local por path não é suportado; informe host/port ou url")
    index = ReadOnlyVectorIndex(QdrantClient(**params), vcfg.get("collection_name") or "openmemory")

    emb = config.get("embedder") or {}
    if (emb.get("provider") or "").lower() == "ollama":
        ensure_ollama_model_present(emb.get("config") or {})
    embedder = EmbedderFactory.create(emb.get("provider"), emb.get("config") or {}, None)

    def embed(text: str) -> list:
        # Mesma chamada de find_near_duplicates.
        return embedder.embed(text, "search")

    return index, embed
