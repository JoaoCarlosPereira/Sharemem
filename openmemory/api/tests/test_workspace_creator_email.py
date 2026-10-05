"""Workspace registra a PESSOA criadora (card 2feaabe4 — kanban por grupo).

``created_by`` mantém a semântica de hostname/ator (owner da busca de specs);
``created_by_email`` é aditivo e guarda o e-mail autenticado quando resolvível.
"""

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import (
    DEFAULT_GROUP_NAME,
    USER_TYPE_LEGACY_HOST,
    Group,
    Machine,
    MachineStatus,
    SpecWorkspace,
    User,
)
from app.routers.specs import get_or_create_workspace, router
from app.utils.logging_context import (
    auth_email_var,
    auth_method_var,
    auth_user_var,
    machine_var,
)
from app.utils.spec_auth import resolve_spec_creator_email


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    yield sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def seeded(factory):
    db = factory()
    group = Group(name=DEFAULT_GROUP_NAME)
    db.add(group)
    db.flush()
    person_id = uuid.uuid4()
    db.add(User(id=person_id, user_id="pessoa", email="pessoa@sysmo.com.br", group_id=group.id))
    db.add(
        User(
            user_id="S0293",
            user_type=USER_TYPE_LEGACY_HOST,
            group_id=group.id,
        )
    )
    db.commit()
    group_id = group.id
    db.close()
    return {"person_id": person_id, "group_id": group_id}


@pytest.fixture
def ctx():
    """Seta contextvars de auth e garante reset."""
    tokens = []

    def _set(*, method="", user="", email="", machine=""):
        tokens.extend(
            [
                (auth_method_var, auth_method_var.set(method)),
                (auth_user_var, auth_user_var.set(user)),
                (auth_email_var, auth_email_var.set(email)),
                (machine_var, machine_var.set(machine)),
            ]
        )

    yield _set
    for var, tok in reversed(tokens):
        var.reset(tok)


def test_sessao_usa_email_do_jwt(factory, seeded, ctx):
    ctx(method="session", user=str(seeded["person_id"]), email="jwt@sysmo.com.br")
    db = factory()
    try:
        assert resolve_spec_creator_email(db) == "jwt@sysmo.com.br"
    finally:
        db.close()


def _add_machine(factory, linked_user_id, status=MachineStatus.linked):
    db = factory()
    db.add(Machine(hostname="S0293", linked_user_id=linked_user_id, status=status))
    db.commit()
    db.close()


@pytest.mark.parametrize(
    "status,expected",
    [(MachineStatus.linked, "pessoa@sysmo.com.br"), (MachineStatus.conflict, None)],
)
def test_agent_token_exige_maquina_linked_ao_dono(factory, seeded, ctx, status, expected):
    _add_machine(factory, seeded["person_id"], status)
    ctx(method="agent_token", user=str(seeded["person_id"]), machine="S0293")
    db = factory()
    try:
        assert resolve_spec_creator_email(db) == expected
    finally:
        db.close()


def test_agent_token_sem_maquina_vinculada_devolve_none(factory, seeded, ctx):
    ctx(method="agent_token", user=str(seeded["person_id"]), machine="S0293")
    db = factory()
    try:
        assert resolve_spec_creator_email(db) is None
    finally:
        db.close()


def test_forja_legado_com_hostname_de_maquina_vinculada_nao_atribui(factory, seeded, ctx):
    """Forja: chamada legacy com o hostname de uma máquina linked no path MCP."""
    _add_machine(factory, seeded["person_id"])
    ctx(method="legacy", machine="S0293")
    db = factory()
    try:
        assert resolve_spec_creator_email(db) is None
    finally:
        db.close()


def test_forja_agent_token_com_maquina_de_outra_pessoa_nao_atribui(factory, seeded, ctx):
    """Forja: token válido de A apontando para a máquina vinculada a B."""
    _add_machine(factory, seeded["person_id"])
    db = factory()
    intruso = uuid.uuid4()
    db.add(User(id=intruso, user_id="intruso", email="intruso@sysmo.com.br"))
    db.commit()
    db.close()
    ctx(method="agent_token", user=str(intruso), machine="S0293")
    db = factory()
    try:
        assert resolve_spec_creator_email(db) is None
    finally:
        db.close()


def test_sem_identidade_devolve_none(factory, seeded, ctx):
    ctx(method="legacy", machine="DESCONHECIDA")
    db = factory()
    try:
        assert resolve_spec_creator_email(db) is None
        assert resolve_spec_creator_email(None) is None
    finally:
        db.close()


def test_rest_grava_pessoa_e_preserva_created_by(factory, seeded, ctx):
    app = FastAPI()
    app.include_router(router)

    def _override():
        sess = factory()
        try:
            yield sess
        finally:
            sess.close()

    app.dependency_overrides[get_db] = _override
    ctx(method="session", user=str(seeded["person_id"]))

    resp = TestClient(app).post(
        "/api/v1/specs/workspaces",
        json={"project_id": "mem0-shared", "slug": "ws-pessoa", "name": "WS"},
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["created_by_email"] == "pessoa@sysmo.com.br"
    db = factory()
    try:
        ws = db.query(SpecWorkspace).filter(SpecWorkspace.slug == "ws-pessoa").one()
        assert ws.created_by_email == "pessoa@sysmo.com.br"
        assert ws.group_id == seeded["group_id"]
    finally:
        db.close()


def test_idempotente_nao_reescreve_criador(factory, seeded):
    db = factory()
    try:
        ws, created = get_or_create_workspace(
            db,
            project_id="mem0-shared",
            slug="ws-x",
            name="X",
            created_by="S0293",
            created_by_email="a@sysmo.com.br",
        )
        assert created is True
        again, created2 = get_or_create_workspace(
            db,
            project_id="mem0-shared",
            slug="ws-x",
            name="X",
            created_by="S0176",
            created_by_email="b@sysmo.com.br",
        )
        assert created2 is False
        assert again.id == ws.id
        assert again.created_by == "S0293"
        assert again.created_by_email == "a@sysmo.com.br"
    finally:
        db.close()
