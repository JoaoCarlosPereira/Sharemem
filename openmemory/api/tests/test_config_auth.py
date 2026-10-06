"""/api/v1/config exige admin e nunca devolve segredos em texto puro.

Card [SEGURANÇA]: antes, qualquer um que alcançasse a porta 8765 lia a
``api_key`` do LLM e podia repontar ``openai_base_url`` para um servidor próprio.
"""

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
from app.middleware.team_auth import AuthMiddleware
from app.models import Base
from app.models import Config as ConfigModel
from app.utils.secret_mask import (
    MASK_PREFIX,
    MaskedSecretError,
    assert_no_masked_secrets,
    find_masked_values,
    is_masked,
    mask_config_secrets,
    restore_masked_secrets,
)
from app.utils.session_jwt import issue_session_jwt

_PATH = Path(__file__).resolve().parents[1] / "app" / "routers" / "config.py"
_spec = importlib.util.spec_from_file_location("config_router_auth_under_test", _PATH)
_config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_config)

ADMIN = "config-admin-token-for-tests"
JWT_SECRET = "segredo-de-teste-com-32-bytes-ok!"
REAL_LLM_KEY = "sk-real-llm-secret-0123456789abcd"
REAL_EMB_KEY = "sk-real-embedder-secret-9876wxyz"
REAL_VS_PASSWORD = "pg-super-secret-password"
ADMIN_HEADERS = {"X-Admin-Token": ADMIN}

SEEDED_CONFIG = {
    "openmemory": {"custom_instructions": None, "multilingual": True},
    "mem0": {
        "llm": {
            "provider": "openai",
            "config": {
                "model": "gpt-oss-20b.gguf",
                "temperature": 0.1,
                "max_tokens": 2000,
                "api_key": REAL_LLM_KEY,
                "openai_base_url": "http://host.docker.internal:8000/v1",
            },
        },
        "embedder": {
            "provider": "openai",
            "config": {"model": "nomic", "api_key": REAL_EMB_KEY},
        },
        "vector_store": {
            "provider": "pgvector",
            "config": {
                "collection_name": "openmemory",
                "host": "mem0_store",
                "user": "mem0",
                "password": REAL_VS_PASSWORD,
                "port": 5432,
            },
        },
    },
}

LLM_BODY = {
    "provider": "openai",
    "config": {"model": "m", "temperature": 0.1, "max_tokens": 10, "api_key": "x"},
}
EMBEDDER_BODY = {"provider": "ollama", "config": {"model": "nomic"}}
VECTOR_STORE_BODY = {"provider": "qdrant", "config": {"collection_name": "openmemory"}}

# (método, caminho, corpo) de TODAS as rotas do router.
ALL_ROUTES = [
    ("GET", "/api/v1/config", None),
    ("GET", "/api/v1/config/", None),
    ("PUT", "/api/v1/config", {"openmemory": {"custom_instructions": "x"}}),
    ("PUT", "/api/v1/config/", {"openmemory": {"custom_instructions": "x"}}),
    ("PATCH", "/api/v1/config", {"openmemory": {"custom_instructions": "x"}}),
    ("PATCH", "/api/v1/config/", {"openmemory": {"custom_instructions": "x"}}),
    ("POST", "/api/v1/config/reset", None),
    ("GET", "/api/v1/config/mem0/llm", None),
    ("PUT", "/api/v1/config/mem0/llm", LLM_BODY),
    ("GET", "/api/v1/config/mem0/embedder", None),
    ("PUT", "/api/v1/config/mem0/embedder", EMBEDDER_BODY),
    ("GET", "/api/v1/config/mem0/vector_store", None),
    ("PUT", "/api/v1/config/mem0/vector_store", VECTOR_STORE_BODY),
    ("GET", "/api/v1/config/openmemory", None),
    ("PUT", "/api/v1/config/openmemory", {"custom_instructions": "x"}),
]


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    maker = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    s = maker()
    s.add(ConfigModel(key="main", value=SEEDED_CONFIG))
    s.commit()
    s.close()
    yield maker
    engine.dispose()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN)
    monkeypatch.setenv("AUTH_JWT_SECRET", JWT_SECRET)
    monkeypatch.delenv("AUTH_ADMIN_EMAILS", raising=False)
    monkeypatch.setattr(_config, "reset_memory_client", lambda: None)


def _client(factory, *, middleware: bool = False) -> TestClient:
    app = FastAPI()
    if middleware:
        app.add_middleware(AuthMiddleware, mode="warn", token_to_team={"tok-alpha": "alpha"})
    app.include_router(_config.router)

    def _override():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override
    return TestClient(app)


def _stored(factory) -> dict:
    s = factory()
    try:
        return s.query(ConfigModel).filter(ConfigModel.key == "main").first().value
    finally:
        s.close()


def _call(client, method, path, body, headers=None):
    return client.request(method, path, json=body, headers=headers or {})


# --- 401 sem credencial -------------------------------------------------------


@pytest.mark.parametrize("method,path,body", ALL_ROUTES)
def test_config_routes_reject_anonymous(factory, method, path, body):
    resp = _call(_client(factory), method, path, body)
    assert resp.status_code == 401
    assert REAL_LLM_KEY not in resp.text


@pytest.mark.parametrize("method,path,body", ALL_ROUTES)
def test_config_routes_reject_legacy_and_team_tokens(factory, method, path, body):
    """Atrás do AuthMiddleware (warn): Bearer local e token de equipe não bastam."""
    client = _client(factory, middleware=True)
    for headers in ({"Authorization": "Bearer local"}, {"X-API-Key": "tok-alpha"}):
        resp = _call(client, method, path, body, headers)
        assert resp.status_code == 401, (headers, resp.text)


def test_config_rejects_wrong_admin_token(factory):
    resp = _client(factory).get("/api/v1/config", headers={"X-Admin-Token": "errado"})
    assert resp.status_code == 401


def test_anonymous_put_does_not_change_llm_base_url(factory):
    body = {
        "mem0": {
            "llm": {
                "provider": "openai",
                "config": {
                    "model": "evil",
                    "temperature": 0.1,
                    "max_tokens": 10,
                    "api_key": "x",
                    "openai_base_url": "http://attacker.example/v1",
                },
            }
        }
    }
    resp = _client(factory).put("/api/v1/config", json=body)
    assert resp.status_code == 401
    stored = _stored(factory)["mem0"]["llm"]["config"]
    assert stored["openai_base_url"] == "http://host.docker.internal:8000/v1"
    assert stored["api_key"] == REAL_LLM_KEY


# --- 200 com credencial admin -------------------------------------------------


@pytest.mark.parametrize("method,path,body", ALL_ROUTES)
def test_config_routes_allow_admin_token(factory, method, path, body):
    resp = _call(_client(factory), method, path, body, ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    assert REAL_LLM_KEY not in resp.text
    assert REAL_EMB_KEY not in resp.text
    assert REAL_VS_PASSWORD not in resp.text


def test_config_allows_admin_bearer_and_session_jwt(factory):
    client = _client(factory, middleware=True)
    resp = client.get("/api/v1/config", headers={"Authorization": f"Bearer {ADMIN}"})
    assert resp.status_code == 200
    jwt = issue_session_jwt(user_id="u1", email="ops@corp.com")
    resp = client.get("/api/v1/config", headers={"Authorization": f"Bearer {jwt}"})
    assert resp.status_code == 200


def test_config_session_outside_admin_allowlist_is_forbidden(factory, monkeypatch):
    monkeypatch.setenv("AUTH_ADMIN_EMAILS", "boss@corp.com")
    client = _client(factory, middleware=True)
    jwt = issue_session_jwt(user_id="u1", email="ops@corp.com")
    resp = client.get("/api/v1/config", headers={"Authorization": f"Bearer {jwt}"})
    assert resp.status_code == 403


# --- mascaramento -------------------------------------------------------------


def test_get_config_masks_all_secrets(factory):
    data = _client(factory).get("/api/v1/config", headers=ADMIN_HEADERS).json()
    llm_key = data["mem0"]["llm"]["config"]["api_key"]
    emb_key = data["mem0"]["embedder"]["config"]["api_key"]
    vs_pwd = data["mem0"]["vector_store"]["config"]["password"]
    assert is_masked(llm_key) and llm_key == f"{MASK_PREFIX}abcd"
    assert is_masked(emb_key) and REAL_EMB_KEY not in emb_key
    assert is_masked(vs_pwd) and REAL_VS_PASSWORD not in vs_pwd
    # campos não-secretos intactos
    assert data["mem0"]["llm"]["config"]["max_tokens"] == 2000
    assert data["mem0"]["llm"]["config"]["openai_base_url"] == "http://host.docker.internal:8000/v1"
    assert data["mem0"]["vector_store"]["config"]["user"] == "mem0"


def test_get_subroutes_mask_secrets(factory):
    client = _client(factory)
    llm = client.get("/api/v1/config/mem0/llm", headers=ADMIN_HEADERS).json()
    emb = client.get("/api/v1/config/mem0/embedder", headers=ADMIN_HEADERS).json()
    vs = client.get("/api/v1/config/mem0/vector_store", headers=ADMIN_HEADERS).json()
    assert is_masked(llm["config"]["api_key"])
    assert is_masked(emb["config"]["api_key"])
    assert is_masked(vs["config"]["password"])


def test_get_does_not_mask_env_reference(factory):
    client = _client(factory)
    body = dict(LLM_BODY, config=dict(LLM_BODY["config"], api_key="env:OPENAI_API_KEY"))
    client.put("/api/v1/config/mem0/llm", json=body, headers=ADMIN_HEADERS)
    data = client.get("/api/v1/config/mem0/llm", headers=ADMIN_HEADERS).json()
    assert data["config"]["api_key"] == "env:OPENAI_API_KEY"


def test_put_with_new_key_persists_and_response_is_masked(factory):
    client = _client(factory)
    new_key = "sk-brand-new-key-0000000000zzzz"
    body = dict(LLM_BODY, config=dict(LLM_BODY["config"], api_key=new_key))
    resp = client.put("/api/v1/config/mem0/llm", json=body, headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    assert new_key not in resp.text
    assert resp.json()["config"]["api_key"] == f"{MASK_PREFIX}zzzz"
    assert _stored(factory)["mem0"]["llm"]["config"]["api_key"] == new_key


# --- ida-e-volta (GET mascarado -> PUT/PATCH) não sobrescreve o segredo ------


def test_roundtrip_put_full_config_keeps_real_secrets(factory):
    client = _client(factory)
    body = client.get("/api/v1/config", headers=ADMIN_HEADERS).json()
    body["mem0"]["llm"]["config"]["model"] = "outro-modelo"
    resp = client.put("/api/v1/config", json=body, headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    assert is_masked(resp.json()["mem0"]["llm"]["config"]["api_key"])
    stored = _stored(factory)["mem0"]
    assert stored["llm"]["config"]["model"] == "outro-modelo"
    assert stored["llm"]["config"]["api_key"] == REAL_LLM_KEY
    assert stored["embedder"]["config"]["api_key"] == REAL_EMB_KEY
    assert stored["vector_store"]["config"]["password"] == REAL_VS_PASSWORD


def test_roundtrip_patch_keeps_real_secret(factory):
    client = _client(factory)
    masked = client.get("/api/v1/config", headers=ADMIN_HEADERS).json()["mem0"]["llm"]
    masked["config"]["temperature"] = 0.7
    resp = client.patch("/api/v1/config", json={"mem0": {"llm": masked}}, headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    stored = _stored(factory)["mem0"]["llm"]["config"]
    assert stored["temperature"] == 0.7
    assert stored["api_key"] == REAL_LLM_KEY


@pytest.mark.parametrize(
    "path,section,field",
    [
        ("/api/v1/config/mem0/llm", "llm", "api_key"),
        ("/api/v1/config/mem0/embedder", "embedder", "api_key"),
        ("/api/v1/config/mem0/vector_store", "vector_store", "password"),
    ],
)
def test_roundtrip_subroute_put_keeps_real_secret(factory, path, section, field):
    client = _client(factory)
    real = _stored(factory)["mem0"][section]["config"][field]
    masked = client.get(path, headers=ADMIN_HEADERS).json()
    assert is_masked(masked["config"][field])
    resp = client.put(path, json=masked, headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    assert _stored(factory)["mem0"][section]["config"][field] == real


def test_roundtrip_env_reference_is_saved_as_is(factory):
    client = _client(factory)
    masked = client.get("/api/v1/config/mem0/llm", headers=ADMIN_HEADERS).json()
    masked["config"]["api_key"] = "env:LLM_API_KEY"
    client.put("/api/v1/config/mem0/llm", json=masked, headers=ADMIN_HEADERS)
    assert _stored(factory)["mem0"]["llm"]["config"]["api_key"] == "env:LLM_API_KEY"


def test_mask_without_real_value_is_dropped_not_persisted(factory):
    """Máscara sem segredo anterior (ex.: provider trocado) nunca é gravada."""
    client = _client(factory)
    body = {"provider": "ollama", "config": {"model": "nomic", "api_key": MASK_PREFIX}}
    s = factory()
    row = s.query(ConfigModel).filter(ConfigModel.key == "main").first()
    value = dict(row.value)
    value["mem0"] = dict(value["mem0"], embedder={"provider": "ollama", "config": {"model": "x"}})
    row.value = value
    s.commit()
    s.close()
    resp = client.put("/api/v1/config/mem0/embedder", json=body, headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    assert "api_key" not in _stored(factory)["mem0"]["embedder"]["config"]


# --- helper -------------------------------------------------------------------


def test_mask_helper_key_matching_and_url_password():
    data = {
        "api_key": "sk-123456789012345",
        "max_tokens": 100,
        "token": "",
        "aws_secret_access_key": "short",
        "redis_url": "redis://user:hunter2@host:6379",
        "url": "http://host:6333",
        "nested": [{"password": "p"}],
    }
    out = mask_config_secrets(data)
    assert out["api_key"] == f"{MASK_PREFIX}2345"
    assert out["max_tokens"] == 100
    assert out["token"] == ""
    assert out["aws_secret_access_key"] == MASK_PREFIX
    assert is_masked(out["redis_url"]) and "hunter2" not in out["redis_url"]
    assert out["url"] == "http://host:6333"
    assert out["nested"][0]["password"] == MASK_PREFIX
    assert data["api_key"] == "sk-123456789012345"  # não muta a entrada


def test_restore_helper_keeps_real_values():
    current = {"llm": {"config": {"api_key": "real", "model": "a"}}}
    incoming = {"llm": {"config": {"api_key": f"{MASK_PREFIX}", "model": "b"}}}
    restore_masked_secrets(incoming, current)
    assert incoming == {"llm": {"config": {"api_key": "real", "model": "b"}}}


# --- listas (I1) --------------------------------------------------------------


def test_mask_strings_inside_lists_url_with_password():
    out = mask_config_secrets({"hosts": ["https://elastic:S3cretPass@es:9200", "http://es2:9200"]})
    assert "S3cretPass" not in str(out)
    assert is_masked(out["hosts"][0])
    assert out["hosts"][1] == "http://es2:9200"


def test_mask_list_under_sensitive_key_and_nested_dicts():
    data = {
        "api_keys": ["sk-aaaaaaaaaaaa1111", "sk-bbbbbbbbbbbb2222"],
        "nodes": [{"host": "n1", "password": "realpass-long-123"}, [{"token": "tok-very-long-xyz9"}]],
    }
    out = mask_config_secrets(data)
    assert out["api_keys"] == [f"{MASK_PREFIX}1111", f"{MASK_PREFIX}2222"]
    assert out["nodes"][0] == {"host": "n1", "password": f"{MASK_PREFIX}-123"}
    assert out["nodes"][1][0]["token"] == f"{MASK_PREFIX}xyz9"
    assert "realpass" not in str(out)


def test_restore_walks_lists_by_index():
    current = {"nodes": [{"password": "realpass-long-123"}, {"password": "other-pass-long-456"}]}
    incoming = mask_config_secrets(current)
    incoming["nodes"][1]["host"] = "novo"
    restore_masked_secrets(incoming, current)
    assert incoming["nodes"][0]["password"] == "realpass-long-123"
    assert incoming["nodes"][1] == {"password": "other-pass-long-456", "host": "novo"}
    assert find_masked_values(incoming) == []


def test_restore_list_of_strings_by_index_when_sizes_match():
    current = {"hosts": ["https://u:pw1-long-secret@a", "https://u:pw2-long-secret@b"]}
    incoming = mask_config_secrets(current)
    restore_masked_secrets(incoming, current)
    assert incoming == current


def test_restore_list_size_mismatch_leaves_mask_for_final_guard():
    current = {"hosts": ["https://u:pw1-long-secret@a"]}
    incoming = {"hosts": [f"{MASK_PREFIX}et@a", "https://es3"]}
    restore_masked_secrets(incoming, current)
    assert find_masked_values(incoming) == ["hosts[0]"]
    with pytest.raises(MaskedSecretError):
        assert_no_masked_secrets(incoming)


ES_STORE = {
    "provider": "elasticsearch",
    "config": {
        "collection_name": "openmemory",
        "hosts": ["https://elastic:S3cretPass@es:9200"],
        "nodes": [{"host": "es", "password": "realpass-long-123"}],
    },
}


def _seed_vector_store(factory, vector_store):
    s = factory()
    row = s.query(ConfigModel).filter(ConfigModel.key == "main").first()
    value = dict(row.value)
    value["mem0"] = dict(value["mem0"], vector_store=vector_store)
    row.value = value
    s.commit()
    s.close()


def test_vector_store_lists_masked_in_get_and_roundtrip_keeps_real(factory):
    _seed_vector_store(factory, ES_STORE)
    client = _client(factory)
    resp = client.get("/api/v1/config/mem0/vector_store", headers=ADMIN_HEADERS)
    assert "S3cretPass" not in resp.text and "realpass-long-123" not in resp.text
    body = resp.json()
    put = client.put("/api/v1/config/mem0/vector_store", json=body, headers=ADMIN_HEADERS)
    assert put.status_code == 200, put.text
    assert _stored(factory)["mem0"]["vector_store"] == ES_STORE
    full = client.get("/api/v1/config", headers=ADMIN_HEADERS)
    assert "S3cretPass" not in full.text and "realpass-long-123" not in full.text


@pytest.mark.parametrize(
    "method,path,body_fn",
    [
        ("PUT", "/api/v1/config/mem0/vector_store", lambda b: b),
        ("PUT", "/api/v1/config", lambda b: {"mem0": {"vector_store": b}}),
        ("PATCH", "/api/v1/config", lambda b: {"mem0": {"vector_store": b}}),
    ],
)
def test_masked_value_without_real_counterpart_is_rejected_422(factory, method, path, body_fn):
    """Garantia absoluta: nenhuma máscara remanescente chega ao banco."""
    _seed_vector_store(factory, ES_STORE)
    before = _stored(factory)
    vs = {
        "provider": "elasticsearch",
        "config": {"hosts": [f"{MASK_PREFIX}9200", "https://outro:9200"]},  # tamanho mudou
    }
    resp = _client(factory).request(method, path, json=body_fn(vs), headers=ADMIN_HEADERS)
    assert resp.status_code == 422, resp.text
    assert "hosts[0]" in resp.json()["detail"]
    assert _stored(factory) == before


def test_save_config_to_db_rejects_any_mask(factory):
    from fastapi import HTTPException

    s = factory()
    try:
        with pytest.raises(HTTPException) as exc:
            _config.save_config_to_db(s, {"mem0": {"llm": {"config": {"x": [{"y": f"{MASK_PREFIX}abcd"}]}}}})
        assert exc.value.status_code == 422
    finally:
        s.close()
    assert _stored(factory) == SEEDED_CONFIG


# --- query string e headers (M6) ----------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://api.example/v1?api_key=sk-query-secret-123",
        "https://api.example/v1?foo=1&token=tok-query-secret-9",
        "https://api.example/v1?password=qs-pass-12345678",
        "https://api.example/v1?X-Api-Key=hdr-in-query-1234",
        "https://api.example/v1?access_token=acc-tok-xxxxxxxx",
        "https://api.example/v1?sig=abc&signature=deadbeefdeadbeef",
    ],
)
def test_mask_url_query_string_secrets(url):
    out = mask_config_secrets({"openai_base_url": url, "list": [url]})
    assert is_masked(out["openai_base_url"])
    assert is_masked(out["list"][0])


def test_url_query_without_secret_is_kept():
    url = "http://host.docker.internal:8000/v1?timeout=30&max_tokens=10"
    assert mask_config_secrets({"openai_base_url": url}) == {"openai_base_url": url}


def test_mask_sensitive_headers_at_any_level():
    data = {
        "llm": {
            "config": {
                "http_client": {
                    "headers": {
                        "Authorization": "Bearer abcdefghijklmnop",
                        "X-Api-Key": "xk-1234567890abcd",
                        "x-auth-token": "short",
                        "Cookie": "session=1",
                        "Content-Type": "application/json",
                    }
                },
                "extra_headers": [{"name": "Proxy-Authorization", "value": "keep-as-is"}],
            }
        }
    }
    out = mask_config_secrets(data)
    headers = out["llm"]["config"]["http_client"]["headers"]
    assert headers["Authorization"] == f"{MASK_PREFIX}mnop"
    assert headers["X-Api-Key"] == f"{MASK_PREFIX}abcd"
    assert headers["x-auth-token"] == MASK_PREFIX
    assert headers["Cookie"] == MASK_PREFIX
    assert headers["Content-Type"] == "application/json"


def test_non_secret_names_are_not_masked():
    data = {"max_tokens": 2000, "tokenizer": "cl100k", "token_limit": "10", "author": "x", "secretary": "y"}
    assert mask_config_secrets(data) == data


# --- trava 422 só nas posições mascaráveis (achado 2) -------------------------

FREE_TEXT = "**** IMPORTANTE: sempre registre decisões de arquitetura"


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("PUT", "/api/v1/config/openmemory", {"custom_instructions": FREE_TEXT}),
        ("PUT", "/api/v1/config", {"openmemory": {"custom_instructions": FREE_TEXT}}),
        ("PATCH", "/api/v1/config", {"openmemory": {"custom_instructions": FREE_TEXT}}),
    ],
)
def test_free_text_starting_with_mask_prefix_is_saved(factory, method, path, body):
    resp = _client(factory).request(method, path, json=body, headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    assert _stored(factory)["openmemory"]["custom_instructions"] == FREE_TEXT
    # Os segredos continuam intactos.
    assert _stored(factory)["mem0"]["llm"]["config"]["api_key"] == REAL_LLM_KEY


def test_find_masked_values_only_in_maskable_positions():
    data = {
        "openmemory": {"custom_instructions": FREE_TEXT, "note": "****abc"},  # texto livre
        "mem0": {
            "llm": {"config": {"api_key": "****qualquer-coisa", "model": "****x-model-name"}},
            "vector_store": {"config": {"hosts": ["****9200"], "url": "****"}},
            "http": {"headers": {"Authorization": "****long-free-form"}},
            "credentials": {"nested": ["****anything goes here"]},  # sob chave sensível
        },
    }
    assert sorted(find_masked_values(data)) == sorted(
        [
            "mem0.llm.config.api_key",
            "mem0.vector_store.config.hosts[0]",
            "mem0.vector_store.config.url",
            "mem0.http.headers.Authorization",
            "mem0.credentials.nested[0]",
        ]
    )


def test_restore_does_not_touch_free_text_and_restores_url_mask():
    current = {
        "custom_instructions": "antigo",
        "redis_url": "redis://u:hunter2-long@h:6379",
        "api_key": REAL_LLM_KEY,
    }
    incoming = mask_config_secrets(current)
    incoming["custom_instructions"] = FREE_TEXT
    restore_masked_secrets(incoming, current)
    assert incoming == {**current, "custom_instructions": FREE_TEXT}


def test_url_mask_without_credentialed_counterpart_is_rejected(factory):
    """Fora de chave sensível, máscara exata sem URL com credencial → 422 (nunca persiste)."""
    before = _stored(factory)
    body = {"mem0": {"llm": dict(SEEDED_CONFIG["mem0"]["llm"])}}
    body["mem0"]["llm"]["config"] = dict(body["mem0"]["llm"]["config"], openai_base_url="****/v1x")
    resp = _client(factory).put("/api/v1/config", json=body, headers=ADMIN_HEADERS)
    assert resp.status_code == 422, resp.text
    assert "openai_base_url" in resp.json()["detail"]
    assert _stored(factory) == before


def test_mask_marker_detection_is_conservative():
    """M7: qualquer valor iniciado com '****' é tratado como máscara (falso positivo aceito)."""
    assert is_masked("****") and is_masked("****abcd") and is_masked("****real-value")
    assert not is_masked("***abc") and not is_masked("env:X") and not is_masked(None)


# --- mascaramento centralizado (M5) -------------------------------------------


def test_new_route_on_router_inherits_masking_and_auth(factory):
    """Rota nova que esquece de mascarar continua sem vazar segredo."""
    from fastapi import APIRouter

    router = APIRouter(
        prefix="/api/v1/config",
        dependencies=_config.router.dependencies,
        route_class=_config.SecretMaskingRoute,
    )

    @router.get("/raw-leak-test")
    async def _raw():
        return {"nested": [{"api_key": REAL_LLM_KEY}], "url": "redis://u:hunter2@h"}

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    assert client.get("/api/v1/config/raw-leak-test").status_code == 401
    resp = client.get("/api/v1/config/raw-leak-test", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    assert REAL_LLM_KEY not in resp.text and "hunter2" not in resp.text
    assert resp.json()["nested"][0]["api_key"] == f"{MASK_PREFIX}abcd"


def test_router_uses_masking_route_for_every_route():
    assert _config.router.routes
    assert all(isinstance(r, _config.SecretMaskingRoute) for r in _config.router.routes)


# --- mensagem neutra (M8) -----------------------------------------------------


def test_401_message_is_neutral(factory):
    detail = _client(factory).get("/api/v1/config").json()["detail"]
    assert detail.startswith("admin credentials required")
    assert "mutation" not in detail
