"""Testes da task_03 (feature auth Google): AuthMiddleware unificado.

App FastAPI isolado (padrão de ``test_team_auth.py``) com uma rota que ecoa as
contextvars de identidade. O lookup de token de agente usa SQLite em memória
via monkeypatch de ``app.database.SessionLocal`` (sem Redis: ``REDIS_URL``
ausente exercita o fallback direto ao banco).
"""

import logging
import os

os.environ.setdefault("OPENAI_API_KEY", "test-key")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.database as database_module
from app.database import Base
from app.middleware.team_auth import AuthMiddleware, TeamAuthMiddleware
from app.models import USER_TYPE_PERSON, AgentToken, User, get_current_utc_time
from app.utils.agent_tokens import hash_token
from app.utils.logging_context import (
    TokenMaskingFilter,
    auth_method_var,
    auth_user_var,
    machine_var,
)
from app.utils.session_jwt import issue_session_jwt

TOKENS = {"tok-alpha": "alpha"}
SECRET = "segredo-de-teste-com-32-bytes-ok!"


def test_auth_middleware_is_pure_asgi_not_base_http():
    """BaseHTTPMiddleware buffers responses and breaks MCP SSE (double response.start)."""
    from starlette.middleware.base import BaseHTTPMiddleware

    assert not issubclass(AuthMiddleware, BaseHTTPMiddleware)
    assert callable(AuthMiddleware(lambda s, r, sn: None))


def _build_app(mode: str) -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware, mode=mode, token_to_team=TOKENS)

    @app.get("/whoami")
    def whoami():
        return {
            "method": auth_method_var.get() or None,
            "user": auth_user_var.get() or None,
            "machine": machine_var.get() or None,
        }

    @app.post("/mcp/claude-code/http/{host}")
    def mcp(host: str):
        return {
            "method": auth_method_var.get() or None,
            "user": auth_user_var.get() or None,
            "machine": machine_var.get() or None,
        }

    return app


@pytest.fixture
def db(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(database_module, "SessionLocal", Session)
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("AUTH_JWT_SECRET", SECRET)
    yield Session
    engine.dispose()


def _seed_person_with_token(Session, raw_token: str):
    db = Session()
    try:
        user = User(user_id="sub-1", user_type=USER_TYPE_PERSON, google_sub="sub-1")
        db.add(user)
        db.flush()
        db.add(
            AgentToken(
                user_id=user.id, token_hash=hash_token(raw_token), prefix="omtk_ab"
            )
        )
        db.commit()
        return str(user.id)
    finally:
        db.close()


class TestAgentTokenResolution:
    def test_query_token_valido_resolve_pessoa_e_maquina(self, db):
        user_id = _seed_person_with_token(db, "omtk_valido123")
        with TestClient(_build_app("warn")) as client:
            body = client.post(
                "/mcp/claude-code/http/DESKTOP-01?token=omtk_valido123"
            ).json()
        assert body == {
            "method": "agent_token",
            "user": user_id,
            "machine": "DESKTOP-01",
        }

    def test_precedencia_query_token_sobre_header_bearer(self, db):
        user_id = _seed_person_with_token(db, "omtk_valido123")
        with TestClient(_build_app("warn")) as client:
            body = client.post(
                "/mcp/claude-code/http/DESKTOP-01?token=omtk_valido123",
                headers={"authorization": "Bearer tok-alpha"},
            ).json()
        assert body["method"] == "agent_token"
        assert body["user"] == user_id

    def test_token_desconhecido_401_mesmo_em_warn(self, db):
        with TestClient(_build_app("warn")) as client:
            resp = client.post("/mcp/claude-code/http/DESKTOP-01?token=omtk_falso")
        assert resp.status_code == 401

    def test_token_revogado_401_mesmo_em_warn(self, db):
        _seed_person_with_token(db, "omtk_valido123")
        session = db()
        try:
            row = session.query(AgentToken).one()
            row.revoked_at = get_current_utc_time()
            session.commit()
        finally:
            session.close()
        with TestClient(_build_app("warn")) as client:
            resp = client.post(
                "/mcp/claude-code/http/DESKTOP-01?token=omtk_valido123"
            )
        assert resp.status_code == 401

    def test_token_por_header_com_prefixo_omtk(self, db):
        user_id = _seed_person_with_token(db, "omtk_valido123")
        with TestClient(_build_app("warn")) as client:
            body = client.get(
                "/whoami", headers={"x-api-key": "omtk_valido123"}
            ).json()
        assert body["method"] == "agent_token"
        assert body["user"] == user_id


class TestSessionJwtResolution:
    def test_jwt_valido_resolve_session(self, db):
        import uuid

        user_pk = uuid.uuid4()
        token = issue_session_jwt(user_id=user_pk, email="a@b.c", name="A")
        with TestClient(_build_app("warn")) as client:
            body = client.get(
                "/whoami", headers={"authorization": f"Bearer {token}"}
            ).json()
        assert body == {"method": "session", "user": str(user_pk), "machine": None}

    def test_jwt_expirado_401_com_headers_cors(self, db, monkeypatch):
        monkeypatch.setenv("AUTH_JWT_TTL_SECONDS", "-10")
        token = issue_session_jwt(user_id="00000000-0000-0000-0000-000000000001")
        with TestClient(_build_app("warn")) as client:
            resp = client.get(
                "/whoami",
                headers={
                    "authorization": f"Bearer {token}",
                    "origin": "http://localhost:3000",
                },
            )
        assert resp.status_code == 401
        assert resp.headers.get("access-control-allow-origin") == "http://localhost:3000"


class TestLegacyAndTeamCompat:
    def test_sem_credencial_em_rota_mcp_passa_como_legacy(self, db):
        with TestClient(_build_app("warn")) as client:
            body = client.post("/mcp/claude-code/http/DESKTOP-01").json()
        assert body["method"] == "legacy"
        assert body["machine"] == "DESKTOP-01"

    def test_token_de_equipe_continua_valido(self, db):
        with TestClient(_build_app("enforce")) as client:
            resp = client.get("/whoami", headers={"x-api-key": "tok-alpha"})
        assert resp.status_code == 200
        assert resp.json()["method"] == "team"

    def test_planka_internal_bearer_accepted_in_enforce(self, db, monkeypatch):
        """PLANKA Spec bridge token must pass AuthMiddleware (Kanban DnD)."""
        monkeypatch.setenv("PLANKA_INTERNAL_ACCESS_TOKEN", "planka-bridge-secret")
        monkeypatch.delenv("INTERNAL_ACCESS_TOKEN", raising=False)
        with TestClient(_build_app("enforce")) as client:
            resp = client.get(
                "/whoami",
                headers={"authorization": "Bearer planka-bridge-secret"},
            )
        assert resp.status_code == 200
        assert resp.json()["method"] == "internal"

    def test_bearer_local_stays_legacy_when_internal_token_collides(
        self, db, monkeypatch
    ):
        """PLANKA_INTERNAL_ACCESS_TOKEN=local must not steal the UI legacy shim."""
        monkeypatch.setenv("PLANKA_INTERNAL_ACCESS_TOKEN", "local")
        monkeypatch.delenv("INTERNAL_ACCESS_TOKEN", raising=False)
        with TestClient(_build_app("warn")) as client:
            resp = client.get(
                "/whoami",
                headers={"authorization": "Bearer local"},
            )
        assert resp.status_code == 200
        assert resp.json()["method"] == "legacy"

    def test_internal_access_token_alias_accepted(self, db, monkeypatch):
        """Fallback INTERNAL_ACCESS_TOKEN also resolves as method=internal."""
        monkeypatch.delenv("PLANKA_INTERNAL_ACCESS_TOKEN", raising=False)
        monkeypatch.setenv("INTERNAL_ACCESS_TOKEN", "alias-bridge-secret")
        with TestClient(_build_app("enforce")) as client:
            resp = client.get(
                "/whoami",
                headers={"authorization": "Bearer alias-bridge-secret"},
            )
        assert resp.status_code == 200
        assert resp.json()["method"] == "internal"

    def test_invalid_team_token_rejected_even_with_internal_configured(
        self, db, monkeypatch
    ):
        monkeypatch.setenv("PLANKA_INTERNAL_ACCESS_TOKEN", "planka-bridge-secret")
        with TestClient(_build_app("enforce")) as client:
            resp = client.get(
                "/whoami",
                headers={"authorization": "Bearer nope-not-a-team-token"},
            )
        assert resp.status_code == 401

    def test_admin_token_bearer_still_works_alongside_internal(
        self, db, monkeypatch
    ):
        monkeypatch.setenv("ADMIN_TOKEN", "admin-secret-token")
        monkeypatch.setenv("PLANKA_INTERNAL_ACCESS_TOKEN", "planka-bridge-secret")
        with TestClient(_build_app("enforce")) as client:
            resp = client.get(
                "/whoami",
                headers={"authorization": "Bearer admin-secret-token"},
            )
        assert resp.status_code == 200
        assert resp.json()["method"] == "admin_token"

    def test_alias_team_auth_middleware_preservado(self):
        assert TeamAuthMiddleware is AuthMiddleware

    def test_redis_indisponivel_cai_para_banco(self, db, monkeypatch):
        # REDIS_URL apontando para host inexistente: o cliente falha e o lookup
        # deve resolver pelo banco sem erro para o chamador.
        monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
        user_id = _seed_person_with_token(db, "omtk_valido123")
        with TestClient(_build_app("warn")) as client:
            body = client.post(
                "/mcp/claude-code/http/DESKTOP-01?token=omtk_valido123"
            ).json()
        assert body["user"] == user_id


class TestTokenMasking:
    def test_filtro_mascara_token_na_mensagem(self):
        record = logging.LogRecord(
            name="x",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg="GET /mcp/c/http/h?token=omtk_segredo123&group=fiscal",
            args=(),
            exc_info=None,
        )
        assert TokenMaskingFilter().filter(record) is True
        assert "omtk_segredo123" not in record.getMessage()
        assert "token=***" in record.getMessage()
        assert "group=fiscal" in record.getMessage()

    def test_filtro_instalado_no_logging_estruturado(self):
        from app.utils.logging_context import install_structured_logging

        install_structured_logging()
        root = logging.getLogger()
        assert any(isinstance(f, TokenMaskingFilter) for f in root.filters)
