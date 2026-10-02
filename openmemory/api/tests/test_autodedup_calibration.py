"""Calibração do limiar com grupos de duplicatas JÁ existentes (somente leitura).

O relatório do modo report só vê pares de escritas novas; estes testes fixam o
caminho que mede grupos já armazenados (Sicredi/TRgn, PATCH 204, Fin104, Fcr722):
mesmo embedder/score do autodedup, matriz intra/entre grupos, intervalo de
limiar com margem e — principalmente — que NADA é escrito (o fake do Qdrant só
expõe ``query_points``/``retrieve`` e falha em qualquer outro método).
"""

import importlib.util
import json
import math
import os
from pathlib import Path

os.environ.setdefault("OPENAI_API_KEY", "test-key")

import pytest
from qdrant_client import models

from app.utils import autodedup_calibration as cal
from app.utils.autodedup_calibration import (
    GroupSpec,
    ReadOnlyVectorIndex,
    calibrate,
    load_groups,
    parse_groups,
    threshold_interval,
)


# --------------------------------------------------------------------------- #
# Fake Qdrant: cosseno real sobre vetores fixos + avaliação dos filtros usados
# --------------------------------------------------------------------------- #
def _norm(v):
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def _cos(a, b):
    return sum(x * y for x, y in zip(_norm(a), _norm(b)))


class _Rec:
    def __init__(self, pid, payload, score=None):
        self.id, self.payload, self.score = pid, payload, score


def _match(cond, pid, payload) -> bool:
    if isinstance(cond, models.Filter):
        must = all(_match(c, pid, payload) for c in cond.must or [])
        should = any(_match(c, pid, payload) for c in cond.should) if cond.should else True
        must_not = not any(_match(c, pid, payload) for c in cond.must_not or [])
        return must and should and must_not
    if isinstance(cond, models.HasIdCondition):
        return pid in {str(i) for i in cond.has_id}
    if isinstance(cond, models.FieldCondition):
        val = payload.get(cond.key)
        m = cond.match
        if isinstance(m, models.MatchValue):
            return val == m.value
        if isinstance(m, models.MatchAny):
            return val in m.any
    raise AssertionError(f"condição inesperada: {cond!r}")


class StrictReadOnlyQdrant:
    """Só leitura: qualquer método fora de query_points/retrieve falha o teste."""

    _ALLOWED = {"query_points", "retrieve"}

    def __init__(self, points):
        object.__setattr__(self, "_points", points)  # id -> (vector, payload)
        object.__setattr__(self, "calls", [])

    def __getattr__(self, name):
        raise AssertionError(f"método não permitido no Qdrant (escrita?): {name}")

    def query_points(self, *, collection_name, query, query_filter=None, limit=10,
                     with_payload=True, with_vectors=False):
        self.calls.append("query_points")
        assert collection_name == "openmemory"
        hits = [
            _Rec(pid, payload, _cos(query, vec))
            for pid, (vec, payload) in self._points.items()
            if query_filter is None or _match(query_filter, pid, payload)
        ]
        hits.sort(key=lambda h: -h.score)
        return type("Resp", (), {"points": hits[:limit]})()

    def retrieve(self, *, collection_name, ids, with_payload=True, with_vectors=False):
        self.calls.append("retrieve")
        return [_Rec(i, self._points[i][1]) for i in ids if i in self._points]


# Vetores 3D: dois "assuntos" vizinhos (boletos) e um distante.
TEXTS = {
    "s1": ("Sicredi 748 usa TRgnFinanceiroBoletoHibrido", [1.0, 0.02, 0.0]),
    "s2": ("Sicredi (748) emite boleto via TRgnFinanceiroBoletoHibrido", [1.0, 0.04, 0.0]),
    "s3": ("Banco 748 Sicredi: classe TRgnFinanceiroBoletoHibrido", [1.0, 0.0, 0.03]),
    "f1": ("Cobranca bancaria na tela Fcr722", [1.0, 0.6, 0.0]),
    "f2": ("Tela Fcr722 faz a cobranca bancaria", [1.0, 0.62, 0.02]),
    "x1": ("Fin104 persiste em GCVPRM02.JSON_DADOS_API", [0.0, 0.0, 1.0]),
    "obs": ("Sicredi 748 TRgn (versao antiga, supersedida)", [1.0, 0.03, 0.01]),
    "q": ("memoria quarentenada", [1.0, 0.02, 0.0]),
}
STATE = {"obs": "obsolete", "q": "quarantined"}


def _store():
    return StrictReadOnlyQdrant(
        {
            pid: (vec, {"data": text, "project": "sysmovs", "state": STATE.get(pid, "active")})
            for pid, (text, vec) in TEXTS.items()
        }
    )


def _embed_calls():
    calls = []
    by_text = {t: v for t, v in TEXTS.values()}

    def embed(text):
        calls.append(text)
        return by_text.get(text, [1.0, 0.03, 0.0])  # queries caem perto de Sicredi

    return embed, calls


def _groups():
    return [
        GroupSpec(name="sicredi", ids=["s1", "s2", "s3"]),
        GroupSpec(name="fcr722", ids=["f1", "f2"]),
    ]


# --------------------------------------------------------------------------- #
class TestCalibrate:
    def test_matriz_e_intervalo_separavel(self):
        store = _store()
        embed, _ = _embed_calls()
        out = calibrate(_groups(), ReadOnlyVectorIndex(store, "openmemory"), embed, margin=0.01)

        c = out["calibration"]
        intra = [p["score"] for p in out["pairs"] if p["kind"] == "intra"]
        inter = [p["score"] for p in out["pairs"] if p["kind"] == "inter"]
        assert len(intra) == 3 + 1 and len(inter) == 3 * 2
        assert c["min_intra"] == min(intra) and c["max_inter"] == max(inter)
        assert c["separable"] is True and c["gap"] > 0
        lo, hi = c["interval"]["min"], c["interval"]["max"]
        assert lo == pytest.approx(c["max_inter"] + 0.01, abs=1e-4) and hi == c["min_intra"]
        assert lo <= c["recommended"] <= hi
        m = out["groups"]["matrix"]
        assert m["sicredi"]["sicredi"] == pytest.approx(
            min(p["score"] for p in out["pairs"] if p["group_a"] == p["group_b"] == "sicredi")
        )
        assert m["sicredi"]["fcr722"] == m["fcr722"]["sicredi"] == c["max_inter"]

    def test_score_igual_ao_do_autodedup(self):
        """Mesmo cosseno que find_near_duplicates veria (embed do texto x vetor salvo)."""
        store = _store()
        embed, _ = _embed_calls()
        out = calibrate(_groups(), ReadOnlyVectorIndex(store, "openmemory"), embed)

        p = next(p for p in out["pairs"] if {p["a"], p["b"]} == {"s1", "f1"})
        expected = _cos(TEXTS["s1"][1], TEXTS["f1"][1])
        assert p["score"] == pytest.approx(round(expected, 4))
        # Intra usa o menor dos dois sentidos; entre grupos, o maior.
        assert p["kind"] == "inter"

    def test_nada_e_escrito(self):
        store = _store()
        embed, _ = _embed_calls()
        calibrate(_groups(), ReadOnlyVectorIndex(store, "openmemory"), embed)
        assert set(store.calls) <= {"query_points", "retrieve"}
        with pytest.raises(AssertionError):
            store.upsert  # noqa: B018 - o fake recusa qualquer escrita

    def test_vizinhos_fora_dos_grupos_e_estado(self):
        store = _store()
        embed, _ = _embed_calls()
        out = calibrate(_groups(), ReadOnlyVectorIndex(store, "openmemory"), embed, top_k=10)

        seen = {r["id"] for rows in out["outside_neighbors"].values() for r in rows}
        # A busca do autodedup esconde obsoletas e quarentenadas.
        assert "obs" not in seen and "q" not in seen
        assert "x1" in seen
        assert out["max_outside_neighbor"] is not None

    def test_membro_obsoleto_ainda_e_medido_quarentenado_nao(self):
        store = _store()
        embed, _ = _embed_calls()
        groups = [GroupSpec(name="sicredi", ids=["s1", "obs", "q"]), GroupSpec(name="x", ids=["x1", "f1"])]
        out = calibrate(groups, ReadOnlyVectorIndex(store, "openmemory"), embed)

        pair_ids = {frozenset((p["a"], p["b"])) for p in out["pairs"]}
        assert frozenset(("s1", "obs")) in pair_ids
        assert not any("q" in ids for ids in pair_ids)
        assert any("q quarentenada" in w for w in out["warnings"])

    def test_grupo_por_query_no_projeto(self):
        store = _store()
        embed, calls = _embed_calls()
        groups = [
            GroupSpec(name="sicredi", query="Sicredi 748 TRgn", project="sysmovs", top=3),
            GroupSpec(name="fcr722", ids=["f1", "f2"]),
        ]
        out = calibrate(groups, ReadOnlyVectorIndex(store, "openmemory"), embed)

        members = {m["id"] for m in out["members"] if m["group"] == "sicredi"}
        assert members == {"s1", "s2", "s3"}  # obsoleta/quarentenada fora
        assert "Sicredi 748 TRgn" in calls

    def test_ids_ausentes_e_repetidos_viram_aviso(self):
        store = _store()
        embed, _ = _embed_calls()
        groups = [GroupSpec(name="a", ids=["s1", "nao-existe"]), GroupSpec(name="b", ids=["s1", "f1"])]
        out = calibrate(groups, ReadOnlyVectorIndex(store, "openmemory"), embed)

        text = " | ".join(out["warnings"])
        assert "nao-existe" in text and "já pertence a a" in text and "menos de 2" in text

    def test_current_threshold_avaliado(self):
        store = _store()
        embed, _ = _embed_calls()
        out = calibrate(
            _groups(), ReadOnlyVectorIndex(store, "openmemory"), embed, current_threshold=0.9999
        )
        assert out["calibration"]["current_ok"] is False  # acima do min intra

    def test_margin_invalida(self):
        with pytest.raises(ValueError):
            calibrate(_groups(), ReadOnlyVectorIndex(_store(), "openmemory"), _embed_calls()[0],
                      margin=float("nan"))


class TestThresholdInterval:
    def test_intervalo_com_margem(self):
        r = threshold_interval(0.97, 0.93, margin=0.01)
        assert r["interval"] == {"min": 0.94, "max": 0.97}
        assert r["recommended"] == 0.97 and r["separable"] is True

    def test_recomendado_arredonda_para_baixo(self):
        r = threshold_interval(0.9765, 0.90, margin=0.01)
        assert r["recommended"] == 0.97

    @pytest.mark.parametrize("hi", [0.57, 0.29, 0.58, 0.95])
    def test_recomendado_exato_nao_perde_um_centesimo(self, hi):
        # 0.57 * 100 == 56.99999999999999: floor puro daria 0.56.
        r = threshold_interval(hi, 0.10, margin=0.01)
        assert r["recommended"] == hi

    def test_sem_folga_nao_recomenda(self):
        r = threshold_interval(0.95, 0.945, margin=0.01)
        assert r["interval"] is None and r["recommended"] is None
        assert r["separable"] is True and "sem limiar seguro" in r["note"]

    def test_sobreposto(self):
        r = threshold_interval(0.93, 0.96)
        assert r["separable"] is False and r["recommended"] is None

    def test_sem_pares(self):
        assert threshold_interval(None, 0.9)["note"]


class TestLoadGroups:
    def test_json_e_yaml(self, tmp_path):
        j = tmp_path / "g.json"
        j.write_text(json.dumps({"groups": [{"name": "a", "ids": ["1", "2"]}]}))
        y = tmp_path / "g.yaml"
        y.write_text("groups:\n  - name: b\n    query: Fcr722\n    project: sysmovs\n    top: 2\n")

        assert load_groups(j)[0].ids == ["1", "2"]
        g = load_groups(y)[0]
        assert (g.name, g.query, g.project, g.top) == ("b", "Fcr722", "sysmovs", 2)

    @pytest.mark.parametrize(
        "data",
        [{"groups": []}, [{"name": "a"}], [{"name": "a", "ids": ["1"]}, {"name": "a", "ids": ["2"]}],
         [{"name": "a", "query": "x", "top": 0}]],
    )
    def test_invalidos(self, data):
        with pytest.raises(ValueError):
            parse_groups(data)

    def test_exemplo_do_runbook_e_valido(self):
        root = Path(__file__).resolve().parents[2]
        example = root / "docs" / "runbooks" / "autodedup-calibration-groups.example.yaml"
        if not example.is_file():
            pytest.skip("exemplo não copiado para a imagem")
        names = [g.name for g in load_groups(example)]
        assert {"sicredi-trgn", "patch-204", "fin104", "fcr722"} <= set(names)


class TestScript:
    def test_script_so_le(self, tmp_path, monkeypatch, capsys):
        root = Path(__file__).resolve().parents[2]
        script = root / "scripts" / "autodedup-calibrate-groups.py"
        if not script.is_file():
            pytest.skip("scripts/ ausente")
        spec = importlib.util.spec_from_file_location("calib_script", script)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        store = _store()
        embed, _ = _embed_calls()
        monkeypatch.setattr(cal, "effective_memory_config", lambda: {})
        monkeypatch.setattr(
            cal, "build_backends", lambda _cfg: (ReadOnlyVectorIndex(store, "openmemory"), embed)
        )
        groups = tmp_path / "g.json"
        groups.write_text(json.dumps({"groups": [
            {"name": "sicredi", "ids": ["s1", "s2"]}, {"name": "fcr", "ids": ["f1", "f2"]}]}))

        assert mod.main([str(groups), "--compact"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["calibration"]["separable"] is True
        assert "current_threshold" in out["calibration"]
        assert set(store.calls) <= {"query_points", "retrieve"}

    def test_build_backends_recusa_qdrant_por_path(self):
        with pytest.raises(RuntimeError, match="path"):
            cal.build_backends({"vector_store": {"provider": "qdrant", "config": {"path": "/tmp/x"}},
                                "embedder": {"provider": "ollama", "config": {}}})

    def test_build_backends_recusa_outro_vector_store(self):
        with pytest.raises(RuntimeError, match="Qdrant"):
            cal.build_backends({"vector_store": {"provider": "chroma", "config": {}}})

    def test_ollama_modelo_ausente_falha_sem_pull(self):
        calls = []

        class FakeClient:
            def __init__(self, host=None):
                calls.append(("init", host))

            def list(self):
                calls.append(("list",))
                return {"models": [{"name": "outro:latest", "model": "outro:latest"}]}

            def pull(self, *_a, **_k):  # pragma: no cover - não pode ser chamado
                raise AssertionError("pull não pode ser chamado")

        with pytest.raises(RuntimeError, match="não baixa modelos"):
            cal.ensure_ollama_model_present(
                {"model": "nomic-embed-text", "ollama_base_url": "http://ollama:11434"}, FakeClient
            )
        assert calls == [("init", "http://ollama:11434"), ("list",)]

    @pytest.mark.parametrize("listed", ["nomic-embed-text:latest", "nomic-embed-text"])
    def test_ollama_modelo_presente_ok(self, listed):
        class FakeClient:
            def __init__(self, host=None):
                pass

            def list(self):
                return {"models": [{"model": listed}]}

        cal.ensure_ollama_model_present({"model": "nomic-embed-text"}, FakeClient)

    def test_build_backends_checa_ollama_antes_do_embedder(self, monkeypatch):
        import qdrant_client

        seen = []
        monkeypatch.setattr(qdrant_client, "QdrantClient", lambda **kw: object())
        monkeypatch.setattr(
            cal, "ensure_ollama_model_present", lambda c: (_ for _ in ()).throw(RuntimeError("ausente"))
        )
        from mem0.utils import factory

        monkeypatch.setattr(factory.EmbedderFactory, "create", lambda *a: seen.append(a))
        with pytest.raises(RuntimeError, match="ausente"):
            cal.build_backends({
                "vector_store": {"provider": "qdrant", "config": {"host": "q", "port": 6333}},
                "embedder": {"provider": "ollama", "config": {"model": "x"}},
            })
        assert seen == []  # o embedder (que faria pull) nem é construído


class TestScriptLayouts:
    """Rodar o script POR CAMINHO (como no runbook) põe só a pasta do script no
    sys.path. Ele precisa achar ``app/`` tanto na imagem quanto no checkout."""

    _SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "autodedup-calibrate-groups.py"
    _APP = Path(__file__).resolve().parents[1] / "app"

    def _layout(self, base: Path, api_subdir: bool, with_app: bool = True) -> Path:
        import shutil

        root = base / "openmemory"
        (root / "scripts").mkdir(parents=True)
        # Cópia (não symlink): o script usa Path(__file__).resolve().
        script = root / "scripts" / self._SCRIPT.name
        shutil.copy(self._SCRIPT, script)
        if with_app:
            api = root / "api" if api_subdir else root
            api.mkdir(exist_ok=True)
            (api / "app").symlink_to(self._APP, target_is_directory=True)
        return script

    def _run(self, script: Path, *args: str, cwd: Path):
        import subprocess
        import sys

        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH",)}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            [sys.executable, str(script), *args],
            cwd=str(cwd), env=env, capture_output=True, text=True, timeout=60,
        )

    @pytest.fixture(autouse=True)
    def _need_script(self):
        if not self._SCRIPT.is_file():
            pytest.skip("scripts/ ausente")

    @pytest.mark.parametrize("api_subdir", [False, True], ids=["imagem", "repo"])
    def test_help_e_validate_only_sem_module_not_found(self, tmp_path, api_subdir):
        # Imagem: /usr/src/openmemory/{app,scripts}; repo: openmemory/{api/app,scripts}.
        script = self._layout(tmp_path / "layout", api_subdir)
        elsewhere = tmp_path / "cwd"
        elsewhere.mkdir()

        r = self._run(script, "--help", cwd=elsewhere)
        assert r.returncode == 0, r.stderr
        assert "ModuleNotFoundError" not in r.stderr
        assert "--validate-only" in r.stdout

        groups = tmp_path / "g.json"
        groups.write_text(json.dumps({"groups": [{"name": "a", "ids": ["x", "y"]}]}))
        r = self._run(script, str(groups), "--validate-only", "--compact", cwd=elsewhere)
        assert r.returncode == 0, r.stderr
        assert "ModuleNotFoundError" not in r.stderr
        assert json.loads(r.stdout) == {"groups": [{"name": "a", "ids": 2, "query": False, "project": None}]}

    def test_controle_sem_app_da_module_not_found(self, tmp_path):
        """Controle: sem ``app/`` acessível o teste acima detectaria a regressão."""
        script = self._layout(tmp_path / "layout", api_subdir=False, with_app=False)
        groups = tmp_path / "g.json"
        groups.write_text(json.dumps({"groups": [{"name": "a", "ids": ["x", "y"]}]}))
        r = self._run(script, str(groups), "--validate-only", cwd=tmp_path)
        assert r.returncode != 0
        assert "ModuleNotFoundError" in r.stderr
