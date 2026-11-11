import hashlib
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import get_db
from app.routers import store as store_router
from app.services.skill_packages import MAX_FILE_BYTES
from app.utils.logging_context import auth_method_var, auth_user_var, team_var
from app.utils.agentregistry import (
    PACKAGE_MEDIA_TYPES,
    AgentRegistryError,
    AgentRegistryRateLimited,
)
from tests.test_app_catalog import FakeRegistry, _db
from tests.test_app_packages import WHEEL_NAME, _card, _wheel

NAME = "horas-app"
URL = f"/api/v1/store/apps/{NAME}"
AUTH = {"Authorization": "Bearer omtk_token"}
WHEEL = _wheel()
OVERSIZED = b"0" * (6 << 20)
EFFECTS_ERROR = "effects.0: Input should be 'somente-leitura', 'grava-arquivos', 'altera-banco' or 'acessa-rede'"


def _build_client(monkeypatch):
    registry = FakeRegistry([])
    app = FastAPI()
    app.include_router(store_router.router)
    app.dependency_overrides[store_router.get_registry_client] = lambda: registry
    app.dependency_overrides[get_db] = lambda: _db([])
    monkeypatch.setattr(store_router, "_registry_auth_headers", lambda request: AUTH)
    test_client = TestClient(app)
    test_client.registry = registry
    return test_client


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(store_router, "_current_actor_id", lambda: "test-actor")
    with _build_client(monkeypatch) as test_client:
        yield test_client


def _put(client, card=None, wheel=WHEEL, name=NAME, wheel_name=WHEEL_NAME):
    data = {} if card is None else {"card": card if isinstance(card, str) else json.dumps(card)}
    files = {} if wheel is None else {"wheel": (wheel_name, wheel, "application/octet-stream")}
    return client.put(f"/api/v1/store/apps/{name}", data=data, files=files)


def _detail(response):
    return response.json()["detail"]


def test_app01_put_multipart_publishes_version_1(client):
    response = _put(client, _card())

    assert response.status_code == 200
    assert response.json() == {
        "name": NAME,
        "version": "1",
        "sha256": hashlib.sha256(WHEEL).hexdigest(),
        "created": True,
    }
    assert set(client.registry.store) == {(NAME, "1"), (NAME, "latest")}
    annotations = client.registry.store[(NAME, "1")]["metadata"]["annotations"]
    assert annotations["sharemem.io/autor"] == "test-actor"
    assert [call[4] for call in client.registry.calls] == [AUTH] * len(client.registry.calls)


def test_app01_put_same_wheel_again_does_not_create_version(client):
    _put(client, _card())

    response = _put(client, _card())

    assert response.status_code == 200
    assert (response.json()["version"], response.json()["created"]) == ("1", False)


def test_app01_put_card_without_usage_returns_422(client):
    card = _card()
    del card["usage"]

    response = _put(client, card)

    assert response.status_code == 422
    assert _detail(response) == "Campo obrigatório ausente: usage"


def test_app01_put_wheel_above_4_mib_returns_413(client):
    response = _put(client, _card(), wheel=OVERSIZED)

    assert response.status_code == 413
    assert _detail(response) == "Arquivo .whl acima de 4 MiB"
    assert client.registry.store == {}


def test_app01_put_reads_at_most_limit_plus_one_byte_of_the_upload(client, monkeypatch):
    seen = []
    real = store_router.AppInput

    class Spy:
        @staticmethod
        def from_upload(card, wheel_name, wheel):
            seen.append(len(wheel))
            return real.from_upload(card, wheel_name, wheel)

    monkeypatch.setattr(store_router, "AppInput", Spy)

    response = _put(client, _card(), wheel=OVERSIZED)

    assert response.status_code == 413
    assert seen == [MAX_FILE_BYTES + 1]


def test_app01_put_without_wheel_part_returns_422(client):
    response = _put(client, _card(), wheel=None)

    assert response.status_code == 422
    assert _detail(response) == "Campo obrigatório ausente: wheel"


def test_app01_put_without_card_part_returns_422(client):
    response = _put(client, None)

    assert response.status_code == 422
    assert _detail(response) == "Campo obrigatório ausente: card"


def test_app01_put_card_that_is_not_json_returns_422(client):
    response = _put(client, "{nao e json")

    assert response.status_code == 422
    assert _detail(response) == "A ficha (card) deve ser um JSON válido"


def test_app01_put_card_that_is_not_a_json_object_returns_422(client):
    response = _put(client, "[1, 2]")

    assert response.status_code == 422
    assert _detail(response) == "A ficha (card) deve ser um objeto JSON"


def test_app01_put_malformed_card_field_returns_422_with_field_path(client):
    response = _put(client, _card(effects=["xpto"]))

    assert response.status_code == 422
    assert _detail(response) == EFFECTS_ERROR
    assert client.registry.store == {}


def test_app01_put_two_malformed_card_fields_are_joined_with_semicolon(client):
    response = _put(client, _card(effects=["xpto"], tags="x"))

    assert response.status_code == 422
    assert _detail(response) == f"{EFFECTS_ERROR}; tags: Input should be a valid list"
    assert client.registry.store == {}


def test_app01_put_card_without_name_returns_the_field_error(client):
    card = _card()
    del card["name"]

    response = _put(client, card)

    assert response.status_code == 422
    assert _detail(response) == "name: Field required"
    assert client.registry.store == {}


@pytest.mark.parametrize("wheel_name", ["app.txt", "dir/horas_app-0.1.0-py3-none-any.whl"])
def test_app01_put_wheel_filename_that_is_not_a_plain_whl_returns_422(client, wheel_name):
    response = _put(client, _card(), wheel_name=wheel_name)

    assert response.status_code == 422
    assert _detail(response) == f"Arquivo do app precisa ser um .whl sem caminho: {wheel_name!r}"
    assert client.registry.store == {}


def test_app01_put_wheel_of_another_project_returns_422(client):
    response = _put(client, _card(), wheel_name="outro_app-0.1.0-py3-none-any.whl")

    assert response.status_code == 422
    assert _detail(response) == "O .whl é do projeto outro_app, não de horas-app"
    assert client.registry.store == {}


@pytest.fixture
def anonymous_context():
    tokens = [(var, var.set("")) for var in (auth_method_var, auth_user_var, team_var)]
    yield
    for var, token in reversed(tokens):
        var.reset(token)


def test_app01_put_without_credential_returns_401_and_never_calls_registry(monkeypatch, anonymous_context):
    monkeypatch.setenv("AUTH_MODE", "enforce")
    with _build_client(monkeypatch) as client:
        response = _put(client, _card())

        assert response.status_code == 401
        assert _detail(response) == "credencial ausente"
        assert client.registry.calls == []


def test_app01_put_store_unavailable_returns_502(client):
    client.registry.error = AgentRegistryError(502, "AgentRegistry indisponível")

    response = _put(client, _card())

    assert response.status_code == 502
    assert _detail(response) == "Store indisponível; tente de novo em instantes"


def test_app01_put_url_name_different_from_card_name_returns_422(client):
    response = _put(client, _card(), name="outro-app")

    assert response.status_code == 422
    assert _detail(response) == "Nome da URL diferente do nome da ficha"
    assert client.registry.store == {}


def test_app01_put_store_rate_limited_returns_429(client):
    client.registry.error = AgentRegistryRateLimited("7")

    response = _put(client, _card())

    assert response.status_code == 429
    assert _detail(response) == "Limite da Store atingido; tente em 7 s"


def test_app02_get_returns_app_view_with_settings_and_commands(client):
    _put(client, _card())

    response = client.get(URL)

    assert response.status_code == 200
    body = response.json()
    assert (body["name"], body["version"], body["is_latest"]) == (NAME, "1", True)
    assert body["wheel_name"] == WHEEL_NAME
    assert body["python_requires"] == ">=3.11"
    assert body["commands"] == {"start": "horas-app", "verify": "horas-app verificar"}
    assert [setting["name"] for setting in body["settings"]] == ["REDMINE_URL", "REDMINE_API_KEY"]
    reads = [call for call in client.registry.calls if call[0] in ("get", "tags")]
    assert {call[0] for call in reads} == {"get", "tags"}
    assert [call[4] for call in reads] == [AUTH] * len(reads)
    assert body["card"]["author"] == "test-actor"
    assert [version["version"] for version in body["versions"]] == ["1"]


def test_app02_get_by_version_returns_that_version(client):
    _put(client, _card())

    response = client.get(f"{URL}/1")

    assert response.status_code == 200
    assert (response.json()["version"], response.json()["is_latest"]) == ("1", True)


def test_app02_get_missing_version_returns_404(client):
    _put(client, _card())

    response = client.get(f"{URL}/9")

    assert response.status_code == 404
    assert _detail(response) == f"Aplicativo não encontrado: {NAME}@9"


def test_app02_get_store_unavailable_returns_502(client):
    client.registry.error = AgentRegistryError(502, "AgentRegistry indisponível")

    response = client.get(URL)

    assert response.status_code == 502
    assert _detail(response) == "Store indisponível; tente de novo em instantes"


def test_app02_get_artifact_returns_registry_bytes_with_localapp_media_type(client):
    _put(client, _card())
    stored = client.registry.artifacts[(NAME, "1")]

    response = client.get(f"{URL}/1/artifact")

    assert response.status_code == 200
    assert response.content == stored
    assert response.headers["content-type"] == PACKAGE_MEDIA_TYPES["app"]
    assert response.headers["content-disposition"] == f'attachment; filename="{NAME}-1.tar.gz"'
    assert response.headers["x-content-type-options"] == "nosniff"
    assert ("artifact", "app", NAME, "1", AUTH) in client.registry.calls


def test_app02_get_missing_artifact_returns_404(client):
    response = client.get(f"{URL}/9/artifact")

    assert response.status_code == 404
    assert _detail(response) == f"Aplicativo não encontrado: {NAME}@9"
