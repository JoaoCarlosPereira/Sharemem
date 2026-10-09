"""Vizinhos ativos do que acabou de ser superado (o alcance que falta ao supersedes).

O ``supersedes`` por ID funciona - verificado em 01/09/2026, a memoria superada
foi para ``state: "obsolete"`` e sumiu das buscas padrao. O problema e o alcance:
a MESMA afirmacao falsa existia em duas gravacoes independentes,

    558075ce-918c-4122-b0c3-26dc873fdd10  (29/07/2026)
    bf2613bf-b10e-49d2-8268-2fa3f6867e52  (06/08/2026)

e superar a primeira por ID deixou a segunda ATIVA. A busca padrao continuaria
devolvendo o dado errado como verdade.

``app.utils.autodedup`` nao cobre este caso: ele procura vizinhos da memoria
NOVA. Quando a nova diz o oposto da antiga - que e o que uma correcao faz - a
duplicata nao e vizinha dela, e vizinha da SUPERADA. E dai que se procura aqui.

Nada e marcado em silencio. A funcao devolve candidatos; quem decide e o
chamador (a resposta do ``mark_obsolete``, o log do worker, a auditoria).
"""

from __future__ import annotations

import logging
import os
from typing import Iterable

logger = logging.getLogger(__name__)

# Limiar de "afirma a mesma coisa". Mais frouxo que o do autodedup (0,95), que
# procura duplicata literal: aqui uma parafrase do mesmo fato ja interessa,
# porque ela sobrevive a correcao e volta na busca como verdade.
SIBLING_SIMILARITY = float(os.getenv("MEM0_SUPERSEDE_SIBLING_SIMILARITY", "0.88"))
SIBLING_TOP_K = int(os.getenv("MEM0_SUPERSEDE_SIBLING_TOP_K", "10"))


def _payload_of(vs, memory_id: str):
    try:
        found = vs.client.retrieve(
            collection_name=vs.collection_name,
            ids=[memory_id],
            with_payload=True,
            with_vectors=False,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("retrieve for sibling lookup failed id=%s: %s", memory_id, exc)
        return None
    if not found:
        return None
    return getattr(found[0], "payload", None) or {}


def find_sibling_candidates(
    memory_client,
    superseded_ids: Iterable[str],
    *,
    threshold: float | None = None,
    top_k: int | None = None,
    exclude_ids: Iterable[str] = (),
) -> list[dict]:
    """Memorias ATIVAS que afirmam o mesmo que as memorias superadas.

    Devolve ``[{"superseded_id", "candidate_id", "score", "candidate_text",
    "project"}]``, ordenado por score decrescente. Nunca levanta: falhar em
    sugerir nao pode derrubar a correcao que ja foi gravada.
    """
    from app.utils.partitioning import bind_active_collection

    threshold = SIBLING_SIMILARITY if threshold is None else threshold
    top_k = SIBLING_TOP_K if top_k is None else top_k

    ids = [str(m).strip() for m in superseded_ids if m and str(m).strip()]
    if not ids:
        return []

    bind_active_collection(memory_client)
    vs = memory_client.vector_store

    # A propria memoria superada e as que o chamador ja tratou saem da lista.
    seen = {str(x).strip() for x in exclude_ids if x} | set(ids)
    found: list[dict] = []

    for mid in ids:
        payload = _payload_of(vs, mid)
        if payload is None:
            continue
        text = (payload.get("data") or "").strip()
        if not text:
            continue
        try:
            vectors = memory_client.embedding_model.embed(text, "search")
            hits = vs.search(query=text, vectors=vectors, top_k=top_k, filters=None)
        except Exception as exc:  # noqa: BLE001
            logger.warning("sibling search failed id=%s: %s", mid, exc)
            continue

        for h in hits or []:
            hid = str(getattr(h, "id", "") or "")
            score = getattr(h, "score", None)
            if not hid or hid in seen:
                continue
            if not isinstance(score, (int, float)) or score < threshold:
                continue
            hp = getattr(h, "payload", {}) or {}
            if (hp.get("state") or "active").lower() != "active":
                continue
            seen.add(hid)
            found.append(
                {
                    "superseded_id": mid,
                    "candidate_id": hid,
                    "score": float(score),
                    "candidate_text": hp.get("data"),
                    "project": hp.get("project"),
                }
            )

    found.sort(key=lambda c: -c["score"])
    return found


def ingest_siblings(memory_client, memory_ids: Iterable[str]) -> list[str]:
    """Os OUTROS fatos nascidos da mesma gravacao que as memorias informadas.

    A extracao e fan-out: um texto vira N fatos atomicos com ids independentes.
    ``ingest_id`` (ver app.utils.scope_keys) e o que permite alcancar os irmaos
    ao superar um deles. Memorias anteriores ao campo nao tem ``ingest_id`` e
    simplesmente nao produzem irmaos - nunca um erro.
    """
    from app.utils.partitioning import bind_active_collection

    ids = [str(m).strip() for m in memory_ids if m and str(m).strip()]
    if not ids:
        return []

    bind_active_collection(memory_client)
    vs = memory_client.vector_store

    ingest_ids = set()
    for mid in ids:
        payload = _payload_of(vs, mid)
        if payload and payload.get("ingest_id"):
            ingest_ids.add(str(payload["ingest_id"]))
    if not ingest_ids:
        return []

    siblings: list[str] = []
    for ingest_id in sorted(ingest_ids):
        try:
            points, _ = vs.client.scroll(
                collection_name=vs.collection_name,
                scroll_filter={
                    "must": [{"key": "ingest_id", "match": {"value": ingest_id}}]
                },
                limit=100,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("ingest sibling scroll failed ingest_id=%s: %s", ingest_id, exc)
            continue
        for p in points or []:
            pid = str(getattr(p, "id", "") or "")
            payload = getattr(p, "payload", {}) or {}
            if not pid or pid in ids:
                continue
            if (payload.get("state") or "active").lower() != "active":
                continue
            siblings.append(pid)
    return siblings
