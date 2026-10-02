"""Regression: PUT /api/v1/config/ must persist and return updated config."""

import importlib.util
import os
from pathlib import Path

os.environ.setdefault("OPENAI_API_KEY", "test-key")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.models import Base

_PATH = Path(__file__).resolve().parents[1] / "app" / "routers" / "config.py"
_spec = importlib.util.spec_from_file_location("config_router_under_test", _PATH)
_config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_config)


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


ADMIN = "test-admin-token"


@pytest.fixture(autouse=True)
def _admin_env(monkeypatch):
    # /api/v1/config exige admin (require_admin); os testes de persistência
    # enviam X-Admin-Token. Proteção coberta em test_config_auth.py.
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN)


def make_client(factory):
    app = FastAPI()
    app.include_router(_config.router)

    def _override():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override
    return TestClient(app, headers={"X-Admin-Token": ADMIN})


def test_put_config_persists_openai_base_url(factory, monkeypatch):
    monkeypatch.setattr(_config, "reset_memory_client", lambda: None)
    client = make_client(factory)

    body = client.get("/api/v1/config").json()
    body["mem0"]["llm"] = {
        "provider": "openai",
        "config": {
            "model": "gpt-oss-20b.gguf",
            "temperature": 0.1,
            "max_tokens": 2000,
            "api_key": "llama.cpp",
            "openai_base_url": "http://host.docker.internal:8000/v1",
        },
    }

    put_resp = client.put("/api/v1/config", json=body)
    assert put_resp.status_code == 200
    saved = put_resp.json()["mem0"]["llm"]["config"]["openai_base_url"]
    assert saved == "http://host.docker.internal:8000/v1"


def test_put_config_persists_llm_model(factory, monkeypatch):
    monkeypatch.setattr(_config, "reset_memory_client", lambda: None)
    client = make_client(factory)

    get_resp = client.get("/api/v1/config")
    assert get_resp.status_code == 200
    body = get_resp.json()
    body["mem0"]["llm"]["config"]["model"] = "gemma-custom.gguf"

    put_resp = client.put("/api/v1/config", json=body)
    assert put_resp.status_code == 200
    assert put_resp.json()["mem0"]["llm"]["config"]["model"] == "gemma-custom.gguf"

    again = client.get("/api/v1/config")
    assert again.json()["mem0"]["llm"]["config"]["model"] == "gemma-custom.gguf"
