"""Alcance do supersedes: irmas ativas e procedencia (rodada 2, itens 2 e 3).

O caso real, de 01/09/2026: a mesma afirmacao falsa existia em duas gravacoes
independentes (558075ce, de 29/07, e bf2613bf, de 06/08). Superar a primeira por
ID deixou a segunda ATIVA, e a busca padrao continuou devolvendo o dado errado.

Sem ``app.utils.supersede_fanout`` estes testes falham: nada no sistema procura
os vizinhos da memoria SUPERADA - o autodedup so olha os vizinhos da NOVA, e uma
correcao diz o oposto da antiga, entao a duplicata nao e vizinha dela.
"""

from types import SimpleNamespace

import pytest

from app.utils.supersede_fanout import find_sibling_candidates, ingest_siblings


def _hit(mid, text, score, state="active", project="sysmo-api-tributacao", ingest_id=None):
    payload = {"data": text, "state": state, "project": project}
    if ingest_id:
        payload["ingest_id"] = ingest_id
    return SimpleNamespace(id=mid, score=score, payload=payload)


class _FakeClient:
    """Qdrant o suficiente para o fan-out: retrieve, search e scroll."""

    def __init__(self, points, hits):
        self._points = {p.id: p for p in points}
        self._hits = hits
        self.collection_name = "c"
        self.embedding_model = SimpleNamespace(embed=lambda text, mode: [0.1, 0.2])
        self.vector_store = SimpleNamespace(
            client=self, collection_name="c", search=self._search
        )

    # --- superficie usada por supersede_fanout
    def retrieve(self, collection_name, ids, with_payload=False, with_vectors=False):
        return [self._points[i] for i in ids if i in self._points]

    def _search(self, query, vectors, top_k, filters=None):
        return self._hits

    def scroll(self, collection_name, scroll_filter, limit, with_payload, with_vectors):
        value = scroll_filter["must"][0]["match"]["value"]
        return (
            [p for p in self._points.values() if p.payload.get("ingest_id") == value],
            None,
        )


@pytest.fixture(autouse=True)
def _sem_bind(monkeypatch):
    """``bind_active_collection`` fala com o Qdrant de verdade; aqui e no-op."""
    import app.utils.partitioning as partitioning

    monkeypatch.setattr(partitioning, "bind_active_collection", lambda c: None)


SUPERADA = _hit("558075ce", "O endpoint retorna cClassTrib, IBGE e aliquotas ao Pricing", 1.0)
IRMA = _hit("bf2613bf", "O endpoint devolve cClassTrib com IBGE e aliquotas ao Pricing", 0.94)
OUTRO_ASSUNTO = _hit("999", "O build nativo usa GraalVM", 0.42)
JA_OBSOLETA = _hit("obs1", "O endpoint retorna cClassTrib e IBGE", 0.97, state="obsolete")
CORRIGIU = _hit("nova", "O MS persiste em tabela e nao retorna a relacao", 0.91)


class TestIrmasAtivas:
    def test_encontra_a_duplicata_que_o_id_nao_alcancava(self):
        client = _FakeClient([SUPERADA], [SUPERADA, IRMA, OUTRO_ASSUNTO])
        out = find_sibling_candidates(client, ["558075ce"])
        assert [c["candidate_id"] for c in out] == ["bf2613bf"]
        assert out[0]["score"] == pytest.approx(0.94)
        assert out[0]["superseded_id"] == "558075ce"

    def test_nao_devolve_a_propria_superada(self):
        client = _FakeClient([SUPERADA], [SUPERADA])
        assert find_sibling_candidates(client, ["558075ce"]) == []

    def test_ignora_quem_ja_esta_obsoleta(self):
        client = _FakeClient([SUPERADA], [JA_OBSOLETA])
        assert find_sibling_candidates(client, ["558075ce"]) == []

    def test_ignora_a_memoria_que_fez_a_correcao(self):
        """A nova costuma ser vizinha da antiga; superar ela seria desfazer tudo."""
        client = _FakeClient([SUPERADA], [CORRIGIU, IRMA])
        out = find_sibling_candidates(client, ["558075ce"], exclude_ids=["nova"])
        assert [c["candidate_id"] for c in out] == ["bf2613bf"]

    def test_abaixo_do_limiar_nao_e_candidata(self):
        client = _FakeClient([SUPERADA], [IRMA])
        assert find_sibling_candidates(client, ["558075ce"], threshold=0.99) == []

    def test_id_inexistente_nao_derruba_a_busca(self):
        client = _FakeClient([SUPERADA], [IRMA])
        out = find_sibling_candidates(client, ["nao-existe", "558075ce"])
        assert [c["candidate_id"] for c in out] == ["bf2613bf"]

    def test_nada_e_marcado_por_conta_propria(self):
        """A funcao so sugere: quem decide e o chamador."""
        client = _FakeClient([SUPERADA], [IRMA])
        find_sibling_candidates(client, ["558075ce"])
        assert IRMA.payload["state"] == "active"


class TestProcedencia:
    def test_alcanca_os_outros_fatos_da_mesma_gravacao(self):
        """Um texto virou tres fatos; superar um tem de poder alcancar os outros."""
        a = _hit("67737eaf", "fato 1", 1.0, ingest_id="job-42")
        b = _hit("4702f46c", "fato 2", 1.0, ingest_id="job-42")
        c = _hit("32015e8e", "fato 3", 1.0, ingest_id="job-42")
        outro = _hit("outro", "fato de outra gravacao", 1.0, ingest_id="job-99")
        client = _FakeClient([a, b, c, outro], [])
        assert sorted(ingest_siblings(client, ["67737eaf"])) == ["32015e8e", "4702f46c"]

    def test_irma_ja_obsoleta_nao_entra(self):
        a = _hit("a", "fato 1", 1.0, ingest_id="job-42")
        b = _hit("b", "fato 2", 1.0, state="obsolete", ingest_id="job-42")
        client = _FakeClient([a, b], [])
        assert ingest_siblings(client, ["a"]) == []

    def test_memoria_anterior_ao_campo_nao_produz_irmas(self):
        """Acervo legado nao tem ingest_id - ausencia nao e erro."""
        legada = _hit("legada", "fato antigo", 1.0)
        client = _FakeClient([legada], [])
        assert ingest_siblings(client, ["legada"]) == []
