"""Relatório consultável do autodedup (``MEM0_AUTODEDUP_MODE=report``).

Antes o modo report só escrevia ``logger.info`` no write-worker, sem acesso
remoto. Estes testes fixam: persistência por candidato (inclusive "quase"
duplicatas entre o piso e o limiar), best-effort (falha de gravação não afeta o
job), ``off``/``apply`` sem gravação e com comportamento inalterado, retenção
restrita à tabela nova, endpoint admin somente leitura com filtros/agregação por
limiar, e a migration ``s1t2u3v4w5x6`` (upgrade/downgrade).
"""

import datetime
import os
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("OPENAI_API_KEY", "test-key")

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import AutodedupReport, WriteQueueJob, WriteQueueStatus
from app.utils import autodedup_report
from app.utils.autodedup import autodedup_after_write, autodedup_report_floor
from app.utils.autodedup_report import (
    prune_reports,
    record_report_candidates,
    summarize,
    summarize_query,
)

ADMIN = "admin-token-de-teste"
ADMIN_HEADERS = {"x-admin-token": ADMIN}


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    yield sessionmaker(autocommit=False, autoflush=False, bind=engine)
    engine.dispose()


@pytest.fixture(autouse=True)
def _env(monkeypatch, factory):
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN)
    monkeypatch.setenv("MEM0_AUTODEDUP_THRESHOLD", "0.95")
    for name in (
        "MEM0_AUTODEDUP_REPORT_FLOOR",
        "MEM0_AUTODEDUP_REPORT_TEXT_CHARS",
        "MEM0_AUTODEDUP_REPORT_RETENTION_DAYS",
        "MEM0_AUTODEDUP_REPORT_MAX_ROWS",
    ):
        monkeypatch.delenv(name, raising=False)
    # Todas as gravações do autodedup vão para o SQLite em memória do teste.
    monkeypatch.setattr("app.database.SessionLocal", factory)
    monkeypatch.setattr(autodedup_report, "_last_prune_monotonic", None)


def _hit(mem_id, score, data="texto existente", project="sysmovs", state="active"):
    return SimpleNamespace(
        id=mem_id, score=score, payload={"data": data, "state": state, "project": project}
    )


def _client(hits):
    client = MagicMock()
    client.embedding_model.embed.return_value = [0.1, 0.2, 0.3]
    client.vector_store.search.return_value = hits
    return client


def _result(*pairs):
    return {"results": [{"id": i, "memory": m, "event": "ADD"} for i, m in pairs]}


def _rows(factory):
    db = factory()
    try:
        return db.query(AutodedupReport).order_by(AutodedupReport.score.desc()).all()
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Persistência no modo report
# --------------------------------------------------------------------------- #
class TestReportPersistence:
    def test_report_persiste_candidato_e_quase_duplicata(self, monkeypatch, factory):
        monkeypatch.setenv("MEM0_AUTODEDUP_MODE", "report")
        client = _client([_hit("dup", 0.97), _hit("quase", 0.90), _hit("longe", 0.70)])

        with patch("app.utils.supersedes.mark_points_obsolete") as mark:
            out = autodedup_after_write(
                client,
                _result(("new", "Sicredi 748 usa TRgnFinanceiroBoletoHibrido")),
                project="sysmovs",
                job_id="job-1",
            )

        mark.assert_not_called()
        # O contrato de candidates (o que apply superseria) não muda.
        assert [c["duplicate_id"] for c in out["candidates"]] == ["dup"]
        assert [c["duplicate_id"] for c in out["near_misses"]] == ["quase"]
        assert out["recorded"] == 2

        rows = _rows(factory)
        assert [(r.duplicate_memory_id, r.above_threshold) for r in rows] == [
            ("dup", True),
            ("quase", False),
        ]
        r = rows[0]
        assert r.job_id == "job-1" and r.project == "sysmovs"
        assert r.new_memory_id == "new" and r.threshold == 0.95
        assert r.duplicate_project == "sysmovs"
        assert r.new_text.startswith("Sicredi 748")
        assert r.created_at is not None

    def test_floor_configuravel_e_limitado_ao_threshold(self, monkeypatch, factory):
        monkeypatch.setenv("MEM0_AUTODEDUP_MODE", "report")
        monkeypatch.setenv("MEM0_AUTODEDUP_REPORT_FLOOR", "0.92")
        client = _client([_hit("a", 0.93), _hit("b", 0.91)])

        autodedup_after_write(client, _result(("new", "x")))

        assert [r.duplicate_memory_id for r in _rows(factory)] == ["a"]
        monkeypatch.setenv("MEM0_AUTODEDUP_REPORT_FLOOR", "0.99")
        assert autodedup_report_floor(0.95) == 0.95

    def test_textos_sao_truncados(self, monkeypatch, factory):
        monkeypatch.setenv("MEM0_AUTODEDUP_MODE", "report")
        monkeypatch.setenv("MEM0_AUTODEDUP_REPORT_TEXT_CHARS", "20")
        client = _client([_hit("dup", 0.99, data="y" * 500)])

        autodedup_after_write(client, _result(("new", "z" * 500)))

        r = _rows(factory)[0]
        assert len(r.new_text) == 20 and r.new_text.endswith("…")
        assert len(r.duplicate_text) == 20

    def test_text_chars_zero_nao_grava_texto(self, monkeypatch, factory):
        monkeypatch.setenv("MEM0_AUTODEDUP_MODE", "report")
        monkeypatch.setenv("MEM0_AUTODEDUP_REPORT_TEXT_CHARS", "0")
        autodedup_after_write(_client([_hit("dup", 0.99)]), _result(("new", "segredo")))

        r = _rows(factory)[0]
        assert r.new_text is None and r.duplicate_text is None

    def test_falha_de_gravacao_nao_levanta(self, monkeypatch):
        monkeypatch.setenv("MEM0_AUTODEDUP_MODE", "report")

        def _boom():
            raise RuntimeError("postgres fora")

        monkeypatch.setattr("app.database.SessionLocal", _boom)
        out = autodedup_after_write(_client([_hit("dup", 0.99)]), _result(("new", "x")))

        assert out["recorded"] == 0
        assert [c["duplicate_id"] for c in out["candidates"]] == ["dup"]

    def test_falha_no_commit_faz_rollback_e_nao_levanta(self):
        session = MagicMock()
        session.commit.side_effect = RuntimeError("deadlock")

        n = record_report_candidates(
            [{"new_id": "n", "duplicate_id": "d", "score": 0.99}],
            threshold=0.95,
            session_factory=lambda: session,
        )

        assert n == 0
        session.rollback.assert_called_once()
        session.close.assert_called_once()


# --------------------------------------------------------------------------- #
# off / apply inalterados
# --------------------------------------------------------------------------- #
class TestOtherModesUnchanged:
    def test_off_nao_grava_nem_busca(self, monkeypatch, factory):
        monkeypatch.delenv("MEM0_AUTODEDUP_MODE", raising=False)
        client = _client([_hit("dup", 0.99)])

        out = autodedup_after_write(client, _result(("new", "x")))

        assert out == {"mode": "off", "candidates": []}
        client.vector_store.search.assert_not_called()
        assert _rows(factory) == []

    def test_apply_nao_grava_e_ignora_floor(self, monkeypatch, factory):
        monkeypatch.setenv("MEM0_AUTODEDUP_MODE", "apply")
        monkeypatch.setenv("MEM0_AUTODEDUP_REPORT_FLOOR", "0.50")
        client = _client([_hit("dup", 0.97), _hit("quase", 0.90)])

        with patch(
            "app.utils.supersedes.mark_points_obsolete",
            return_value={"updated": ["dup"], "missing": []},
        ) as mark:
            out = autodedup_after_write(client, _result(("new", "x")))

        assert mark.call_count == 1
        assert mark.call_args.args[1] == ["dup"]
        assert out["superseded"] == ["dup"]
        assert "near_misses" not in out and "recorded" not in out
        assert _rows(factory) == []


# --------------------------------------------------------------------------- #
# Integração com o write-worker: o job termina done mesmo se o relatório falhar
# --------------------------------------------------------------------------- #
class TestWriteWorkerIntegration:
    @pytest.mark.asyncio
    async def test_job_done_mesmo_com_falha_ao_gravar_relatorio(self, monkeypatch, tmp_path):
        from app.utils.write_queue import WriteJob, WriteQueue
        from app.workers.write_worker import WriteWorker

        engine = create_engine(
            f"sqlite:///{tmp_path / 'wq.db'}", connect_args={"check_same_thread": False}
        )
        WriteQueueJob.__table__.create(bind=engine, checkfirst=True)
        qfactory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        queue = WriteQueue(session_factory=qfactory)

        monkeypatch.setenv("MEM0_AUTODEDUP_MODE", "report")

        def _boom():
            raise RuntimeError("tabela autodedup_reports ausente")

        monkeypatch.setattr("app.database.SessionLocal", _boom)

        client = _client([_hit("dup", 0.99)])

        async def _add(text, **kwargs):
            return {"results": [{"id": "nova", "memory": text, "event": "ADD"}]}

        client.add = MagicMock(side_effect=_add)
        worker = WriteWorker(
            queue=queue, client_provider=lambda: client, upsert_project=lambda *a, **k: None
        )
        job_id = queue.enqueue(
            WriteJob(
                id=str(uuid.uuid4()), project="p", hostname="h", client_name="c",
                text="um fato", created_at="",
            )
        )

        assert await worker.process_once() == 1
        client.vector_store.search.assert_called()  # o autodedup rodou
        db = qfactory()
        try:
            row = db.query(WriteQueueJob).filter(WriteQueueJob.id == uuid.UUID(job_id)).one()
            assert row.status == WriteQueueStatus.done
        finally:
            db.close()
        engine.dispose()


# --------------------------------------------------------------------------- #
# Retenção (somente autodedup_reports)
# --------------------------------------------------------------------------- #
def _add_row(db, *, score=0.96, created_at=None, project="sysmovs", new="n", dup=None):
    db.add(
        AutodedupReport(
            created_at=created_at or datetime.datetime(2026, 10, 1, 12, 0, 0),
            job_id="j",
            project=project,
            new_memory_id=new,
            duplicate_memory_id=dup or uuid.uuid4().hex,
            score=score,
            threshold=0.95,
            above_threshold=score >= 0.95,
        )
    )


class TestRetention:
    def test_prune_por_idade_e_por_teto(self, factory):
        now = datetime.datetime(2026, 10, 2, 12, 0, 0)
        db = factory()
        _add_row(db, created_at=now - datetime.timedelta(days=40), dup="velho")
        for i in range(5):
            _add_row(db, created_at=now - datetime.timedelta(minutes=i), dup=f"r{i}")
        db.add(WriteQueueJob(project="p", hostname="h", client_name="c", text="t"))
        db.commit()

        removed = prune_reports(db, retention_days=30, max_rows=3, now=now)

        assert removed == 3  # 1 por idade + 2 excedentes (os mais antigos)
        left = {r.duplicate_memory_id for r in db.query(AutodedupReport).all()}
        assert left == {"r0", "r1", "r2"}
        # Nada fora da tabela nova é tocado.
        assert db.query(WriteQueueJob).count() == 1
        db.close()

    def test_zero_desliga_limites(self, factory):
        now = datetime.datetime(2026, 10, 2)
        db = factory()
        _add_row(db, created_at=now - datetime.timedelta(days=400))
        db.commit()
        assert prune_reports(db, retention_days=0, max_rows=0, now=now) == 0
        assert db.query(AutodedupReport).count() == 1
        db.close()

    def test_gravacao_aplica_retencao_por_teto(self, monkeypatch, factory):
        monkeypatch.setenv("MEM0_AUTODEDUP_REPORT_MAX_ROWS", "2")
        cands = [{"new_id": "n", "duplicate_id": f"d{i}", "score": 0.99} for i in range(4)]

        record_report_candidates(cands, threshold=0.95)

        assert len(_rows(factory)) == 2


# --------------------------------------------------------------------------- #
# Agregação
# --------------------------------------------------------------------------- #
class TestSummarize:
    def test_histograma_e_limiares(self):
        rows = [(0.951, "n1", "d1"), (0.95, "n2", "d1"), (0.99, "n3", "d3"),
                (1.0, "n4", "d4"), (0.87, "n5", "d5"), (0.5, "n6", "d6")]

        s = summarize(rows, current_threshold=0.95)

        hist = {h["min"]: h["count"] for h in s["histogram"]}
        assert len(s["histogram"]) == 15 and s["histogram"][-1]["max"] == 1.0
        assert hist[0.95] == 2 and hist[0.99] == 2 and hist[0.87] == 1
        assert s["below_histogram"] == 1 and s["total_pairs"] == 6
        th = {t["threshold"]: t for t in s["thresholds"]}
        assert th[0.95]["pairs"] == 4
        assert th[0.95]["would_supersede"] == 3  # d1 aparece duas vezes
        assert th[0.95]["current"] is True
        assert th[0.99]["pairs"] == 2
        assert th[0.85]["pairs"] == 5

    def test_threshold_atual_fora_da_grade_entra_na_tabela(self):
        s = summarize([(0.955, "n", "d")], current_threshold=0.955)
        th = {t["threshold"]: t for t in s["thresholds"]}
        assert th[0.955]["current"] is True and th[0.955]["pairs"] == 1

    def test_current_nao_se_perde_com_mais_de_4_casas(self):
        # Antes: round(t, 4) virava 0.9512 e nenhum limiar ficava current=True.
        s = summarize([(0.95124, "n", "d"), (0.95122, "n2", "d2")], current_threshold=0.95123)
        cur = [t for t in s["thresholds"] if t["current"]]
        assert len(cur) == 1 and cur[0]["threshold"] == 0.95123
        assert cur[0]["pairs"] == 1  # mesma comparação exata do apply

    def test_comparacao_exata_como_o_apply(self):
        # float32 do Qdrant: 0.94999998 NÃO é supersedido com limiar 0.95.
        s = summarize([(0.94999998, "n", "d")], current_threshold=0.95)
        th = {t["threshold"]: t["pairs"] for t in s["thresholds"]}
        hist = {h["min"]: h["count"] for h in s["histogram"]}
        assert th[0.95] == 0 and hist[0.94] == 1 and hist[0.95] == 0

    def test_limiares_abaixo_do_piso_sao_marcados(self):
        s = summarize([(0.96, "n", "d")], current_threshold=0.95, report_floor=0.90)
        th = {t["threshold"]: t for t in s["thresholds"]}
        assert th[0.89]["below_report_floor"] is True
        assert th[0.90]["below_report_floor"] is False
        assert s["report_floor"] == 0.90
        hist = {h["min"]: h for h in s["histogram"]}
        assert hist[0.85]["below_report_floor"] and not hist[0.90]["below_report_floor"]

    def test_sql_igual_ao_calculo_em_memoria(self, factory):
        rows = [(0.951, "n1", "d1"), (0.95, "n2", "d1"), (0.99, "n3", "d3"), (1.0, "n4", "d4"),
                (0.87, "n5", "d5"), (0.5, "n6", "d6"), (0.94999998, "n7", "d7"), (0.95123, "n8", "d8")]
        db = factory()
        for s, n, d in rows:
            _add_row(db, score=s, new=n, dup=d)
        db.commit()
        for thr in (0.95, 0.95123, 0.9):
            in_sql = summarize_query(db.query(AutodedupReport), current_threshold=thr, report_floor=0.88)
            assert in_sql == summarize(rows, current_threshold=thr, report_floor=0.88)
        db.close()


# --------------------------------------------------------------------------- #
# Endpoint
# --------------------------------------------------------------------------- #
def _api(factory):
    from app.routers.admin_autodedup import router

    app = FastAPI()
    app.include_router(router)

    def _override():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override
    return TestClient(app)


@pytest.fixture
def seeded(factory):
    db = factory()
    base = datetime.datetime(2026, 9, 1)
    _add_row(db, score=0.97, project="sysmovs", created_at=base, dup="a")
    _add_row(db, score=0.93, project="sysmovs", created_at=base + datetime.timedelta(days=10), dup="b")
    _add_row(db, score=0.88, project="mem0-shared", created_at=base + datetime.timedelta(days=20), dup="c")
    db.commit()
    db.close()
    return factory


class TestEndpoint:
    def test_exige_admin(self, seeded):
        with _api(seeded) as c:
            assert c.get("/admin/autodedup/report").status_code == 401
            assert c.get(
                "/admin/autodedup/report", headers={"authorization": "Bearer local"}
            ).status_code == 401
            assert c.get(
                "/admin/autodedup/report", headers={"x-admin-token": "errado"}
            ).status_code == 401

    def test_sem_filtros_lista_e_agrega(self, seeded):
        with _api(seeded) as c:
            body = c.get("/admin/autodedup/report", headers=ADMIN_HEADERS).json()

        assert [i["duplicate_memory_id"] for i in body["items"]] == ["a", "b", "c"]
        assert body["config"]["threshold"] == 0.95
        assert body["summary"]["total_pairs"] == 3
        th = {t["threshold"]: t["pairs"] for t in body["summary"]["thresholds"]}
        assert th[0.95] == 1 and th[0.93] == 2 and th[0.88] == 3 and th[0.98] == 0

    def test_filtros(self, seeded):
        with _api(seeded) as c:
            by_project = c.get(
                "/admin/autodedup/report", params={"project": "sysmovs"}, headers=ADMIN_HEADERS
            ).json()
            by_score = c.get(
                "/admin/autodedup/report", params={"min_score": 0.9}, headers=ADMIN_HEADERS
            ).json()
            by_since = c.get(
                "/admin/autodedup/report",
                params={"since": "2026-09-05T00:00:00Z"},
                headers=ADMIN_HEADERS,
            ).json()
            limited = c.get(
                "/admin/autodedup/report", params={"limit": 1}, headers=ADMIN_HEADERS
            ).json()

        assert {i["duplicate_memory_id"] for i in by_project["items"]} == {"a", "b"}
        assert {i["duplicate_memory_id"] for i in by_score["items"]} == {"a", "b"}
        assert {i["duplicate_memory_id"] for i in by_since["items"]} == {"b", "c"}
        # limit corta só a listagem; a agregação continua sobre tudo que foi filtrado.
        assert len(limited["items"]) == 1 and limited["summary"]["total_pairs"] == 3

    def test_filtros_max_score_e_above_threshold(self, seeded):
        with _api(seeded) as c:
            faixa = c.get(
                "/admin/autodedup/report",
                params={"min_score": 0.9, "max_score": 0.97},
                headers=ADMIN_HEADERS,
            ).json()
            acima = c.get(
                "/admin/autodedup/report", params={"above_threshold": "true"}, headers=ADMIN_HEADERS
            ).json()
            quase = c.get(
                "/admin/autodedup/report", params={"above_threshold": "false"}, headers=ADMIN_HEADERS
            ).json()

        assert [i["duplicate_memory_id"] for i in faixa["items"]] == ["b"]
        assert faixa["summary"]["total_pairs"] == 1 and faixa["filters"]["max_score"] == 0.97
        assert [i["duplicate_memory_id"] for i in acima["items"]] == ["a"]
        assert {i["duplicate_memory_id"] for i in quase["items"]} == {"b", "c"}

    def test_resumo_marca_piso_e_nao_expoe_mode(self, seeded, monkeypatch):
        monkeypatch.setenv("MEM0_AUTODEDUP_REPORT_FLOOR", "0.90")
        with _api(seeded) as c:
            body = c.get("/admin/autodedup/report", headers=ADMIN_HEADERS).json()

        assert body["config"]["report_floor"] == 0.90
        th = {t["threshold"]: t for t in body["summary"]["thresholds"]}
        assert th[0.88]["below_report_floor"] is True and th[0.95]["below_report_floor"] is False
        assert "mode" not in body["items"][0]

    def test_agregacao_nao_carrega_linhas(self, seeded, monkeypatch):
        """O resumo vem de uma única consulta agregada (sem .all() das linhas)."""
        from sqlalchemy.orm import Query

        calls = []
        real_all = Query.all

        def _spy(self):
            calls.append(self)
            return real_all(self)

        monkeypatch.setattr(Query, "all", _spy)
        with _api(seeded) as c:
            body = c.get("/admin/autodedup/report", params={"limit": 0}, headers=ADMIN_HEADERS).json()

        assert body["summary"]["total_pairs"] == 3 and body["items"] == []
        assert calls == []

    def test_limit_invalido_422(self, seeded):
        with _api(seeded) as c:
            r = c.get("/admin/autodedup/report", params={"limit": 5000}, headers=ADMIN_HEADERS)
        assert r.status_code == 422

    def test_rota_registrada_no_app_principal(self):
        # Verificação estática: importar ``main`` liga tracing/DB de verdade.
        src = (Path(__file__).resolve().parents[1] / "main.py").read_text()
        assert "app.include_router(admin_autodedup_router)" in src
        from app.routers import admin_autodedup_router

        paths = {r.path for r in admin_autodedup_router.routes}
        assert paths == {"/admin/autodedup/report"}
        assert {m for r in admin_autodedup_router.routes for m in r.methods} == {"GET"}


# --------------------------------------------------------------------------- #
# Migration
# --------------------------------------------------------------------------- #
class TestMigration:
    @staticmethod
    def _cfg(tmp_path, monkeypatch):
        from alembic.config import Config

        db_path = tmp_path / "autodedup.db"
        url = f"sqlite:///{db_path}"
        monkeypatch.setenv("DATABASE_URL", url)
        ini = tmp_path / "alembic.ini"
        ini.write_text(
            "[alembic]\nscript_location = alembic\n"
            "sqlalchemy.url = driver://user:pass@localhost/dbname\n\n"
            "[loggers]\nkeys = root\n\n[handlers]\nkeys = console\n\n"
            "[formatters]\nkeys = generic\n\n"
            "[logger_root]\nlevel = WARN\nhandlers = console\n\n"
            "[handler_console]\nclass = StreamHandler\nargs = (sys.stderr,)\n"
            "level = NOTSET\nformatter = generic\n\n"
            "[formatter_generic]\nformat = %(levelname)s %(message)s\n"
        )
        cfg = Config(str(ini))
        cfg.set_main_option(
            "script_location", str(Path(__file__).resolve().parents[1] / "alembic")
        )
        return cfg, url

    _DEV_DDL = (
        "CREATE TABLE autodedup_reports (id CHAR(32) NOT NULL PRIMARY KEY, "
        "created_at DATETIME NOT NULL, job_id VARCHAR, mode VARCHAR NOT NULL, "
        "project VARCHAR, new_memory_id VARCHAR NOT NULL, duplicate_memory_id VARCHAR NOT NULL, "
        "duplicate_project VARCHAR, score FLOAT NOT NULL, threshold FLOAT NOT NULL, "
        "above_threshold BOOLEAN NOT NULL, new_text VARCHAR, duplicate_text VARCHAR)"
    )

    def test_upgrade_recria_tabela_de_dev_com_mode_not_null(self, tmp_path, monkeypatch):
        """Versão de dev criava ``mode NOT NULL`` → INSERTs do modelo falhariam
        em silêncio. O upgrade recria só essa tabela; as demais ficam intactas."""
        from alembic import command

        cfg, url = self._cfg(tmp_path, monkeypatch)
        command.upgrade(cfg, "r0s1t2u3v4w5")
        eng = create_engine(url)
        with eng.begin() as conn:
            conn.execute(sa.text(self._DEV_DDL))
            conn.execute(sa.text("CREATE INDEX ix_autodedup_reports_mode ON autodedup_reports (mode)"))
            conn.execute(sa.text(
                "INSERT INTO autodedup_reports VALUES ('a', '2026-01-01', NULL, 'report', 'p', "
                "'n', 'd', NULL, 0.9, 0.95, 0, NULL, NULL)"))
            conn.execute(sa.text(
                "INSERT INTO write_queue (id, project, hostname, client_name, text, "
                "status, attempts) VALUES (:i, 'p', 'h', 'c', 't', 'done', 0)"), {"i": uuid.uuid4().hex})
        before = set(sa.inspect(eng).get_table_names())
        eng.dispose()

        command.upgrade(cfg, "s1t2u3v4w5x6")
        eng = create_engine(url)
        insp = sa.inspect(eng)
        assert set(insp.get_table_names()) == before  # nenhuma tabela a mais/menos
        assert {c["name"] for c in insp.get_columns("autodedup_reports")} == {
            c.name for c in AutodedupReport.__table__.columns
        }
        assert "ix_autodedup_reports_mode" not in {i["name"] for i in insp.get_indexes("autodedup_reports")}
        with eng.connect() as conn:
            assert conn.execute(sa.text("SELECT count(*) FROM write_queue")).scalar() == 1
            assert conn.execute(sa.text("SELECT count(*) FROM autodedup_reports")).scalar() == 0
        eng.dispose()

        # O modelo volta a conseguir gravar.
        eng = create_engine(url)
        db = sessionmaker(bind=eng)()
        try:
            db.add(AutodedupReport(
                new_memory_id="n", duplicate_memory_id="d", score=0.9, threshold=0.95,
                above_threshold=False,
            ))
            db.commit()
            assert db.query(AutodedupReport).count() == 1
        finally:
            db.close()
            eng.dispose()

    def test_upgrade_preserva_tabela_compativel(self, tmp_path, monkeypatch):
        """Tabela já no schema final (ex.: criada por create_all) não é recriada."""
        from alembic import command

        cfg, url = self._cfg(tmp_path, monkeypatch)
        command.upgrade(cfg, "r0s1t2u3v4w5")
        eng = create_engine(url)
        AutodedupReport.__table__.create(bind=eng)
        db = sessionmaker(bind=eng)()
        db.add(AutodedupReport(new_memory_id="n", duplicate_memory_id="d", score=0.9,
                               threshold=0.95, above_threshold=False))
        db.commit()
        db.close()
        eng.dispose()

        command.upgrade(cfg, "s1t2u3v4w5x6")
        eng = create_engine(url)
        with eng.connect() as conn:
            assert conn.execute(sa.text("SELECT count(*) FROM autodedup_reports")).scalar() == 1
        eng.dispose()

    def test_upgrade_cria_tabela_e_downgrade_remove(self, tmp_path, monkeypatch):
        from alembic import command
        from alembic.config import Config

        db_path = tmp_path / "autodedup.db"
        url = f"sqlite:///{db_path}"
        monkeypatch.setenv("DATABASE_URL", url)
        ini = tmp_path / "alembic.ini"
        ini.write_text(
            "[alembic]\nscript_location = alembic\n"
            "sqlalchemy.url = driver://user:pass@localhost/dbname\n\n"
            "[loggers]\nkeys = root\n\n[handlers]\nkeys = console\n\n"
            "[formatters]\nkeys = generic\n\n"
            "[logger_root]\nlevel = WARN\nhandlers = console\n\n"
            "[handler_console]\nclass = StreamHandler\nargs = (sys.stderr,)\n"
            "level = NOTSET\nformatter = generic\n\n"
            "[formatter_generic]\nformat = %(levelname)s %(message)s\n"
        )
        cfg = Config(str(ini))
        cfg.set_main_option(
            "script_location", str(Path(__file__).resolve().parents[1] / "alembic")
        )

        command.upgrade(cfg, "r0s1t2u3v4w5")
        eng = create_engine(url)
        assert "autodedup_reports" not in sa.inspect(eng).get_table_names()
        with eng.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO write_queue (id, project, hostname, client_name, text, "
                    "status, attempts) VALUES (:i, 'p', 'h', 'c', 't', 'done', 0)"
                ),
                {"i": uuid.uuid4().hex},
            )

        command.upgrade(cfg, "s1t2u3v4w5x6")
        insp = sa.inspect(eng)
        assert "autodedup_reports" in insp.get_table_names()
        idx = {i["name"] for i in insp.get_indexes("autodedup_reports")}
        assert idx == {
            "ix_autodedup_reports_created_at",
            "ix_autodedup_reports_score",
            "idx_autodedup_reports_project_time",
        }
        # Migration e modelo declaram os mesmos índices.
        assert {i.name for i in AutodedupReport.__table__.indexes} == idx
        cols = {c["name"] for c in insp.get_columns("autodedup_reports")}
        assert {c.name for c in AutodedupReport.__table__.columns} == cols

        command.downgrade(cfg, "r0s1t2u3v4w5")
        eng.dispose()
        eng = create_engine(url)
        assert "autodedup_reports" not in sa.inspect(eng).get_table_names()
        with eng.connect() as conn:
            assert conn.execute(sa.text("SELECT count(*) FROM write_queue")).scalar() == 1
        eng.dispose()

    def test_migration_e_head_unico(self):
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config()
        cfg.set_main_option(
            "script_location", str(Path(__file__).resolve().parents[1] / "alembic")
        )
        script = ScriptDirectory.from_config(cfg)
        heads = script.get_heads()
        assert len(heads) == 1, heads
        assert script.get_revision("s1t2u3v4w5x6").down_revision == "r0s1t2u3v4w5"
        # A migration faz parte da cadeia do head único (direta ou via merge revision).
        ancestors = {rev.revision for rev in script.iterate_revisions(heads[0], "base")}
        assert "s1t2u3v4w5x6" in ancestors
