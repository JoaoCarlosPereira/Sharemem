"""Arquivamento não destrutivo de cards do Kanban (``archive_task``/``unarchive_task``).

Alternativa ao ``delete_task``: o card some da listagem padrão e do quadro, mas
coluna, assignee, histórico de status e comentários continuam no banco. Cobre
o núcleo (``task_lock``), as tools MCP, os endpoints REST, os efeitos colaterais
(timeout worker, lifecycle do workspace, espelho/bridge PLANKA) e a migration.
As invariantes da TechSpec são verificadas explicitamente: exclusividade do
claim (ADR-003), concorrência otimista com conflict (ADR-005) e card arquivado
fora do pipeline até ser desarquivado.
"""

import json
import os
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("OPENAI_API_KEY", "test-key")

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import (
    CommentTargetType,
    Project,
    SpecAuditLog,
    SpecComment,
    SpecWorkspace,
    SpecWorkspaceStatus,
    TaskCard,
    TaskCardStatus,
    TaskStatusHistory,
    get_current_utc_time,
)
from app.utils import task_lock
from app.utils.task_lock import (
    TaskStatusPolicyError,
    archive_task,
    claim_task,
    release_task,
    unarchive_task,
    update_task_status,
)


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    yield sessionmaker(autocommit=False, autoflush=False, bind=engine)
    engine.dispose()


@pytest.fixture
def db(factory):
    session = factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(autouse=True)
def _mirror_off(monkeypatch):
    monkeypatch.setenv("PLANKA_MIRROR_SYNC", "0")


def _mk_ws(db, slug="arch-ws"):
    if not db.query(Project).filter(Project.name == "mem0-shared").first():
        db.add(Project(name="mem0-shared"))
        db.commit()
    ws = SpecWorkspace(project_id="mem0-shared", slug=slug, name="Arch")
    db.add(ws)
    db.commit()
    db.refresh(ws)
    return ws


def _mk_task(db, ws, **kwargs):
    kwargs.setdefault("status", TaskCardStatus.tasks)
    task = TaskCard(workspace_id=ws.id, title=kwargs.pop("title", "Card"), **kwargs)
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


# ---------------------------------------------------------------------------
# Núcleo (task_lock)
# ---------------------------------------------------------------------------


class TestArchiveCore:
    def test_arquiva_preservando_coluna_historico_e_comentarios(self, db):
        ws = _mk_ws(db)
        task = _mk_task(db, ws)
        claimed = claim_task(db, task.id, "host-a")
        db.add(
            SpecComment(
                target_type=CommentTargetType.task,
                target_id=task.id,
                author="host-a",
                body="progresso",
            )
        )
        db.commit()

        result = archive_task(db, task.id, claimed.version, "host-a", reason="cancelado")

        assert result.updated is True and result.conflict is False
        assert result.version == claimed.version + 1
        assert result.archived_at is not None
        assert result.archived_by == "host-a"
        db.expire_all()
        fresh = db.get(TaskCard, task.id)
        assert fresh.status == TaskCardStatus.em_andamento
        assert fresh.assignee == "host-a"
        assert db.query(TaskStatusHistory).filter_by(task_id=task.id).count() == 1
        assert db.query(SpecComment).filter_by(target_id=task.id).count() == 1
        audit = db.query(SpecAuditLog).filter_by(action="archive_task").one()
        assert audit.detail["reason"] == "cancelado"
        assert audit.detail["status"] == "em_andamento"

    def test_conflito_de_versao_nao_altera_nada(self, db):
        ws = _mk_ws(db)
        task = _mk_task(db, ws)

        result = archive_task(db, task.id, task.version + 7, "host-a")

        assert result.conflict is True and result.updated is False
        assert result.version == task.version
        db.expire_all()
        fresh = db.get(TaskCard, task.id)
        assert fresh.archived_at is None
        assert fresh.version == task.version
        assert db.query(SpecAuditLog).filter_by(action="archive_task").count() == 0

    def test_card_ativo_de_terceiro_e_recusado(self, db):
        ws = _mk_ws(db)
        task = _mk_task(db, ws)
        claimed = claim_task(db, task.id, "host-a")

        with pytest.raises(TaskStatusPolicyError) as exc:
            archive_task(db, task.id, claimed.version, "host-b")
        assert exc.value.code == "not_assignee"
        with pytest.raises(TaskStatusPolicyError):
            archive_task(db, task.id, claimed.version, None)
        db.expire_all()
        assert db.get(TaskCard, task.id).archived_at is None

    @pytest.mark.parametrize("status", [TaskCardStatus.tasks, TaskCardStatus.concluido])
    def test_card_fora_de_coluna_ativa_qualquer_um_arquiva(self, db, status):
        ws = _mk_ws(db)
        task = _mk_task(db, ws, status=status, assignee="host-a")

        result = archive_task(db, task.id, task.version, "host-b")

        assert result.updated is True

    def test_arquivar_duas_vezes_e_desarquivar_ativo_sao_recusados(self, db):
        ws = _mk_ws(db)
        task = _mk_task(db, ws)
        with pytest.raises(TaskStatusPolicyError) as exc:
            unarchive_task(db, task.id, task.version, "host-a")
        assert exc.value.code == "not_archived"

        result = archive_task(db, task.id, task.version, "host-a")
        with pytest.raises(TaskStatusPolicyError) as exc:
            archive_task(db, task.id, result.version, "host-a")
        assert exc.value.code == "already_archived"

    def test_task_inexistente_levanta_value_error(self, db):
        with pytest.raises(ValueError):
            archive_task(db, uuid.uuid4(), 1, "host-a")

    def test_card_arquivado_nao_pode_ser_assumido_movido_nem_liberado(self, db):
        ws = _mk_ws(db)
        task = _mk_task(db, ws)
        claimed = claim_task(db, task.id, "host-a")
        archived = archive_task(db, task.id, claimed.version, "host-a")

        claim = claim_task(db, task.id, "host-a")
        assert claim.claimed is False and claim.archived is True

        with pytest.raises(TaskStatusPolicyError) as exc:
            update_task_status(
                db, task.id, TaskCardStatus.revisao_codigo, archived.version, "host-a"
            )
        assert exc.value.code == "archived"
        # Nem o caminho confiável do bridge PLANKA (enforce_policy=False) move.
        with pytest.raises(TaskStatusPolicyError):
            update_task_status(
                db,
                task.id,
                TaskCardStatus.revisao_codigo,
                archived.version,
                "ui-user",
                enforce_policy=False,
            )
        with pytest.raises(TaskStatusPolicyError):
            release_task(db, task.id, "host-a")
        db.expire_all()
        fresh = db.get(TaskCard, task.id)
        assert fresh.status == TaskCardStatus.em_andamento
        assert fresh.version == archived.version

    def test_desarquivar_volta_na_mesma_coluna_e_segue_o_pipeline(self, db):
        ws = _mk_ws(db)
        task = _mk_task(db, ws)
        claimed = claim_task(db, task.id, "host-a")
        archived = archive_task(db, task.id, claimed.version, "host-a")

        restored = unarchive_task(db, task.id, archived.version, "host-b")

        assert restored.updated is True
        assert restored.archived_at is None and restored.archived_by is None
        db.expire_all()
        fresh = db.get(TaskCard, task.id)
        assert fresh.status == TaskCardStatus.em_andamento
        assert fresh.assignee == "host-a"
        # Pipeline continua obrigatório após desarquivar: não pula colunas.
        with pytest.raises(TaskStatusPolicyError) as exc:
            update_task_status(db, task.id, TaskCardStatus.concluido, fresh.version, "host-a")
        assert exc.value.code == "skip_pipeline"
        moved = update_task_status(
            db, task.id, TaskCardStatus.revisao_codigo, fresh.version, "host-a"
        )
        assert moved.updated is True

    def test_desarquivar_renova_atividade_para_o_timeout(self, db):
        ws = _mk_ws(db)
        old = get_current_utc_time() - timedelta(days=5)
        task = _mk_task(
            db,
            ws,
            status=TaskCardStatus.em_andamento,
            assignee="host-a",
            version=2,
            last_activity_at=old,
        )
        archived = archive_task(db, task.id, 2, "host-a")
        unarchive_task(db, task.id, archived.version, "host-a")
        db.expire_all()
        # SQLite devolve datetime naive; compara no mesmo referencial (UTC).
        assert db.get(TaskCard, task.id).last_activity_at.replace(tzinfo=None) > old.replace(
            tzinfo=None
        )

    def test_desarquivar_com_versao_antiga_e_conflito(self, db):
        ws = _mk_ws(db)
        task = _mk_task(db, ws)
        stale_version = task.version
        archived = archive_task(db, task.id, stale_version, "host-a")

        result = unarchive_task(db, task.id, stale_version, "host-a")

        assert result.conflict is True
        assert result.version == archived.version
        db.expire_all()
        assert db.get(TaskCard, task.id).archived_at is not None


# ---------------------------------------------------------------------------
# Efeitos colaterais: timeout worker, lifecycle do workspace, PLANKA
# ---------------------------------------------------------------------------


class TestArchiveSideEffects:
    def test_timeout_worker_ignora_card_arquivado(self, factory, db):
        from app.workers.spec_task_timeout_worker import SpecTaskTimeoutWorker

        ws = _mk_ws(db)
        task = _mk_task(
            db,
            ws,
            status=TaskCardStatus.em_andamento,
            assignee="host-a",
            version=2,
            last_activity_at=get_current_utc_time() - timedelta(hours=72),
        )
        worker = SpecTaskTimeoutWorker(timeout_hours=24, session_factory=factory)
        assert len(worker.eligible_tasks(db)) == 1

        archive_task(db, task.id, 2, "host-a")

        assert worker.eligible_tasks(db) == []

    def test_card_arquivado_nao_segura_o_workspace_aberto(self, db):
        ws = _mk_ws(db)
        _mk_task(db, ws, status=TaskCardStatus.concluido, title="feito")
        pendente = _mk_task(db, ws, title="cancelado")

        archive_task(db, pendente.id, pendente.version, "host-a")

        db.expire_all()
        assert db.get(SpecWorkspace, ws.id).status == SpecWorkspaceStatus.concluido

    def test_desarquivar_card_aberto_reabre_workspace_concluido(self, db):
        ws = _mk_ws(db)
        _mk_task(db, ws, status=TaskCardStatus.concluido, title="feito")
        pendente = _mk_task(db, ws, title="cancelado")
        archived = archive_task(db, pendente.id, pendente.version, "host-a")
        db.expire_all()
        assert db.get(SpecWorkspace, ws.id).status == SpecWorkspaceStatus.concluido

        unarchive_task(db, pendente.id, archived.version, "host-a")

        db.expire_all()
        assert db.get(SpecWorkspace, ws.id).status == SpecWorkspaceStatus.ativo

    def test_espelho_planka_remove_ao_arquivar_e_recria_ao_desarquivar(self, db, monkeypatch):
        monkeypatch.setenv("PLANKA_MIRROR_SYNC", "1")
        ws = _mk_ws(db)
        task = _mk_task(db, ws)
        client = MagicMock()
        client.delete_task = AsyncMock()
        client.mirror_task = AsyncMock()
        client.set_project_lifecycle = AsyncMock()
        with patch("app.utils.planka_hooks.PlankaMirrorHttpClient", return_value=client):
            archived = archive_task(db, task.id, task.version, "host-a")
            client.delete_task.assert_awaited_once_with(task.id)
            client.mirror_task.assert_not_awaited()

            unarchive_task(db, task.id, archived.version, "host-a")
            client.mirror_task.assert_awaited_once_with(task.id)

    def test_falha_do_espelho_nao_desfaz_o_arquivamento(self, db, monkeypatch):
        from app.utils.planka import PlankaMirrorError

        monkeypatch.setenv("PLANKA_MIRROR_SYNC", "1")
        ws = _mk_ws(db)
        task = _mk_task(db, ws)
        client = MagicMock()
        client.delete_task = AsyncMock(side_effect=PlankaMirrorError(503, "down"))
        client.set_project_lifecycle = AsyncMock()
        with patch("app.utils.planka_hooks.PlankaMirrorHttpClient", return_value=client):
            result = archive_task(db, task.id, task.version, "host-a")

        assert result.updated is True
        db.expire_all()
        assert db.get(TaskCard, task.id).archived_at is not None

    def test_bridge_planka_ignora_movimento_de_card_arquivado(self, db):
        from app.models import SpecPlankaIdMap
        from app.utils.planka import ENTITY_TASK
        from app.utils.planka_bridge import apply_planka_card_move

        ws = _mk_ws(db)
        task = _mk_task(db, ws)
        archived = archive_task(db, task.id, task.version, "host-a")
        db.add(SpecPlankaIdMap(entity_type=ENTITY_TASK, spec_id=task.id, planka_id="card-1"))
        db.add(
            SpecPlankaIdMap(
                entity_type="list:em_andamento", spec_id=ws.id, planka_id="list-1"
            )
        )
        db.commit()

        out = apply_planka_card_move(
            db, planka_card_id="card-1", planka_list_id="list-1", actor="ui-user"
        )

        assert out == {"applied": False, "reason": "archived", "task_id": str(task.id)}
        db.expire_all()
        fresh = db.get(TaskCard, task.id)
        assert fresh.status == TaskCardStatus.tasks
        assert fresh.version == archived.version

    @pytest.mark.asyncio
    async def test_resync_planka_nao_recria_card_arquivado(self, db):
        from app.utils.planka_resync import resync_workspace

        ws = _mk_ws(db)
        ativo = _mk_task(db, ws, title="ativo")
        arquivado = _mk_task(db, ws, title="arquivado")
        archive_task(db, arquivado.id, arquivado.version, "host-a")
        client = MagicMock()
        client.ensure_workspace_board = AsyncMock(return_value="board-1")
        client.mirror_task = AsyncMock()
        client.mirror_document = AsyncMock()
        client.set_project_lifecycle = AsyncMock()

        await resync_workspace(db, ws.id, client=client)

        mirrored = [c.args[0] for c in client.mirror_task.await_args_list]
        assert mirrored == [ativo.id]


# ---------------------------------------------------------------------------
# REST (/api/v1/specs)
# ---------------------------------------------------------------------------


@pytest.fixture
def client(factory):
    from app.models import DEFAULT_GROUP_NAME, Group, User
    from app.routers.specs import router
    from app.utils.logging_context import auth_method_var, auth_user_var

    s = factory()
    person_id = uuid.uuid4()
    try:
        g = Group(name=DEFAULT_GROUP_NAME)
        s.add(g)
        s.flush()
        s.add(User(id=person_id, user_id="ui-user", email="t@t.com", group_id=g.id))
        s.commit()
    finally:
        s.close()

    app = FastAPI()
    app.include_router(router)

    def _override():
        sess = factory()
        try:
            yield sess
        finally:
            sess.close()

    app.dependency_overrides[get_db] = _override
    tok_u = auth_user_var.set(str(person_id))
    tok_m = auth_method_var.set("session")
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        auth_user_var.reset(tok_u)
        auth_method_var.reset(tok_m)


def _rest_ws_task(client):
    ws = client.post(
        "/api/v1/specs/workspaces",
        json={"project_id": "mem0-shared", "slug": "arch-rest", "name": "Arch"},
    ).json()
    task = client.post(
        "/api/v1/specs/tasks", json={"workspace_id": ws["id"], "title": "Card"}
    ).json()
    return ws, task


class TestArchiveRest:
    def test_arquivar_esconde_da_listagem_e_do_quadro(self, client):
        ws, task = _rest_ws_task(client)

        r = client.post(
            f"/api/v1/specs/tasks/{task['id']}/archive",
            json={"expected_version": task["version"], "actor": "A", "reason": "obsoleto"},
        )

        assert r.status_code == 200
        body = r.json()
        assert body["archived_at"] is not None
        assert body["archived_by"] == "A"
        assert body["version"] == task["version"] + 1
        listed = client.get(f"/api/v1/specs/workspaces/{ws['id']}/tasks").json()
        assert listed == []
        everything = client.get(
            f"/api/v1/specs/workspaces/{ws['id']}/tasks", params={"include_archived": True}
        ).json()
        assert [t["id"] for t in everything] == [task["id"]]
        assert everything[0]["archived_at"] is not None
        board = client.get(f"/api/v1/specs/workspaces/{ws['id']}").json()
        assert task["id"] not in json.dumps(board)
        # O card continua acessível diretamente (nada foi apagado).
        assert client.get(f"/api/v1/specs/tasks/{task['id']}").status_code == 200

    def test_conflito_de_versao_devolve_409(self, client):
        _, task = _rest_ws_task(client)

        r = client.post(
            f"/api/v1/specs/tasks/{task['id']}/archive",
            json={"expected_version": 99, "actor": "A"},
        )

        assert r.status_code == 409
        assert r.json()["detail"]["conflict"] is True
        assert r.json()["detail"]["current_version"] == task["version"]

    def test_card_ativo_de_terceiro_devolve_409_policy(self, client):
        _, task = _rest_ws_task(client)
        claimed = client.post(
            f"/api/v1/specs/tasks/{task['id']}/claim", json={"claimant": "A"}
        ).json()

        r = client.post(
            f"/api/v1/specs/tasks/{task['id']}/archive",
            json={"expected_version": claimed["version"], "actor": "B"},
        )

        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "not_assignee"

    def test_claim_de_card_arquivado_devolve_409_archived(self, client):
        _, task = _rest_ws_task(client)
        client.post(
            f"/api/v1/specs/tasks/{task['id']}/archive",
            json={"expected_version": task["version"], "actor": "A"},
        )

        r = client.post(f"/api/v1/specs/tasks/{task['id']}/claim", json={"claimant": "A"})

        assert r.status_code == 409
        assert r.json()["detail"]["archived"] is True

    def test_desarquivar_devolve_card_a_listagem(self, client):
        ws, task = _rest_ws_task(client)
        archived = client.post(
            f"/api/v1/specs/tasks/{task['id']}/archive",
            json={"expected_version": task["version"], "actor": "A"},
        ).json()

        r = client.post(
            f"/api/v1/specs/tasks/{task['id']}/unarchive",
            json={"expected_version": archived["version"], "actor": "A"},
        )

        assert r.status_code == 200
        assert r.json()["archived_at"] is None
        listed = client.get(f"/api/v1/specs/workspaces/{ws['id']}/tasks").json()
        assert [t["id"] for t in listed] == [task["id"]]

    def test_task_inexistente_devolve_404(self, client):
        r = client.post(
            f"/api/v1/specs/tasks/{uuid.uuid4()}/archive", json={"expected_version": 1}
        )
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------


@pytest.fixture
def mcp(factory, monkeypatch):
    from app import mcp_server
    from app.models import DEFAULT_GROUP_NAME, Group, User
    from app.utils.logging_context import auth_method_var, machine_var

    monkeypatch.setattr(mcp_server, "SessionLocal", factory)
    auth_method_var.set("legacy")
    machine_var.set("DESKTOP-01")
    s = factory()
    try:
        g = Group(name=DEFAULT_GROUP_NAME)
        s.add(g)
        s.flush()
        s.add(User(id=uuid.uuid4(), user_id="DESKTOP-01", group_id=g.id, user_type="legacy_host"))
        s.commit()
    finally:
        s.close()
    mcp_server.user_id_var.set("DESKTOP-01")
    mcp_server.client_name_var.set("cursor")
    mcp_server.auth_method_var.set("legacy")
    mcp_server.auth_user_var.set("")
    return mcp_server


class TestArchiveMcp:
    @pytest.mark.asyncio
    async def test_fluxo_completo_via_mcp(self, mcp):
        ws = json.loads(await mcp.create_spec_workspace("mem0-shared", "arch-mcp", "Arch"))
        task = json.loads(await mcp.create_task(ws["id"], "Card"))
        claimed = json.loads(await mcp.claim_task(task["id"]))

        out = json.loads(
            await mcp.archive_task(task["id"], claimed["version"], reason="cancelado")
        )

        assert out["archived"] is True
        assert out["status"] == "em_andamento"
        assert out["version"] == claimed["version"] + 1
        assert out["archived_at"] is not None
        default = json.loads(await mcp.list_tasks(ws["id"]))["results"]
        assert default == []
        everything = json.loads(await mcp.list_tasks(ws["id"], include_archived=True))["results"]
        assert [t["id"] for t in everything] == [task["id"]]
        assert everything[0]["archived_at"] is not None

        again = json.loads(await mcp.claim_task(task["id"]))
        assert again["claimed"] is False and again["archived"] is True
        assert "unarchive_task" in again["message"]

        restored = json.loads(await mcp.unarchive_task(task["id"], out["version"]))
        assert restored["archived"] is False
        assert restored["archived_at"] is None
        default = json.loads(await mcp.list_tasks(ws["id"]))["results"]
        assert [t["id"] for t in default] == [task["id"]]

    @pytest.mark.asyncio
    async def test_conflito_e_politica_sao_erros_estruturados(self, mcp):
        ws = json.loads(await mcp.create_spec_workspace("mem0-shared", "arch-mcp2", "Arch"))
        task = json.loads(await mcp.create_task(ws["id"], "Card"))

        conflict = json.loads(await mcp.archive_task(task["id"], 42))
        assert conflict["conflict"] is True
        assert conflict["current_version"] == task["version"]

        policy = json.loads(await mcp.unarchive_task(task["id"], task["version"]))
        assert policy["policy"] is True and policy["code"] == "not_archived"

    @pytest.mark.asyncio
    async def test_card_ativo_de_outro_agente_e_recusado(self, mcp):
        ws = json.loads(await mcp.create_spec_workspace("mem0-shared", "arch-mcp3", "Arch"))
        task = json.loads(await mcp.create_task(ws["id"], "Card"))
        mcp.user_id_var.set("host-a")
        from app.utils.logging_context import machine_var

        machine_var.set("host-a")
        claimed = json.loads(await mcp.claim_task(task["id"]))
        assert claimed["claimed"] is True

        mcp.user_id_var.set("host-b")
        machine_var.set("host-b")
        out = json.loads(await mcp.archive_task(task["id"], claimed["version"]))

        assert out["policy"] is True and out["code"] == "not_assignee"

    @pytest.mark.asyncio
    async def test_task_inexistente_devolve_error(self, mcp):
        out = await mcp.archive_task(str(uuid.uuid4()), 1)
        assert out.startswith("Error:")


# ---------------------------------------------------------------------------
# Migration Alembic (aditiva e reversível)
# ---------------------------------------------------------------------------


class TestArchiveMigration:
    def test_upgrade_adiciona_colunas_e_downgrade_remove_sem_perder_cards(
        self, tmp_path, monkeypatch
    ):
        from alembic import command
        from alembic.config import Config

        db_path = tmp_path / "archive.db"
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

        command.upgrade(cfg, "q9r0s1t2u3v4")
        eng = create_engine(url)
        cols = {c["name"] for c in sa.inspect(eng).get_columns("task_cards")}
        assert "archived_at" not in cols
        ws_id = uuid.uuid4().hex
        task_id = uuid.uuid4().hex
        with eng.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO spec_workspaces (id, project_id, slug, name, status) "
                    "VALUES (:i, 'p', 's', 'n', 'planejamento')"
                ),
                {"i": ws_id},
            )
            conn.execute(
                sa.text(
                    "INSERT INTO task_cards (id, workspace_id, title, status, version, "
                    "is_blocked) VALUES (:i, :w, 'card', 'tasks', 1, 0)"
                ),
                {"i": task_id, "w": ws_id},
            )

        command.upgrade(cfg, "r0s1t2u3v4w5")
        insp = sa.inspect(eng)
        cols = {c["name"] for c in insp.get_columns("task_cards")}
        assert {"archived_at", "archived_by"} <= cols
        assert "ix_task_cards_archived_at" in {i["name"] for i in insp.get_indexes("task_cards")}
        with eng.connect() as conn:
            row = conn.execute(
                sa.text("SELECT title, archived_at FROM task_cards WHERE id = :i"),
                {"i": task_id},
            ).one()
        assert row.title == "card" and row.archived_at is None

        command.downgrade(cfg, "q9r0s1t2u3v4")
        eng.dispose()
        eng = create_engine(url)
        cols = {c["name"] for c in sa.inspect(eng).get_columns("task_cards")}
        assert "archived_at" not in cols and "archived_by" not in cols
        with eng.connect() as conn:
            assert conn.execute(sa.text("SELECT count(*) FROM task_cards")).scalar() == 1
        eng.dispose()


def test_api_publica_do_modulo():
    assert callable(task_lock.archive_task)
    assert callable(task_lock.unarchive_task)
