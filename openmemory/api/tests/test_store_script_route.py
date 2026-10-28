from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.models import ScriptDownload
from app.routers.store import get_registry_client, router
from app.services.script_packages import ScriptInput, content_sha256, to_registry_resource
from app.utils.agentregistry import AgentRegistryError, AgentRegistryResourceNotFound

NAME = "gcenfe02-xml"
CONTENT = "print('v1')\n"


class FakeRegistryClient:
    def __init__(self):
        self.store = {}
        self.error = None

    async def get_resource(self, *, kind, name, tag=None, namespace="default", auth_headers=None):
        if self.error:
            raise self.error
        if (name, tag) not in self.store:
            raise AgentRegistryResourceNotFound()
        return self.store[(name, tag)]

    async def list_tags(self, *, kind, name, namespace="default", auth_headers=None):
        return {"items": [res for (stored, _), res in self.store.items() if stored == name]}


def _publish(registry):
    script = ScriptInput(
        name=NAME,
        file_name="gcenfe02_xml.py",
        description="Decodifica a coluna GCENFE02.XML",
        usage="python gcenfe02_xml.py <arquivo>",
        language="python",
        effects=["somente-leitura"],
        area="sysmo-s1",
        content=CONTENT,
    )
    annotations = {
        "sharemem.io/versao": "1",
        "sharemem.io/autor": "pessoa-1",
        "sharemem.io/publicado-em": "2026-10-27T00:00:00+00:00",
        "sharemem.io/sha256": content_sha256(CONTENT),
    }
    for tag in ("1", "latest"):
        registry.store[(NAME, tag)] = to_registry_resource(script, tag, annotations)


def _app_client():
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    ScriptDownload.__table__.create(engine)
    db = Session(engine)
    registry = FakeRegistryClient()
    _publish(registry)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_registry_client] = lambda: registry
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app), registry, db


def test_scr22_get_script_returns_script_view_of_latest():
    client, _, db = _app_client()

    response = client.get(f"/api/v1/store/scripts/{NAME}")

    assert response.status_code == 200
    body = response.json()
    assert (body["name"], body["version"], body["is_latest"]) == (NAME, "1", True)
    assert (body["content"], body["sha256"]) == (CONTENT, content_sha256(CONTENT))
    assert body["card"]["usage"] == "python gcenfe02_xml.py <arquivo>"
    assert body["card"]["author"] == "pessoa-1"
    assert body["card"]["downloads_30d"] == 0
    assert [version["version"] for version in body["versions"]] == ["1"]
    assert db.scalars(select(ScriptDownload)).all() == []


def test_scr22_get_script_by_version_returns_that_version():
    client, _, _ = _app_client()

    response = client.get(f"/api/v1/store/scripts/{NAME}/1")

    assert response.status_code == 200
    assert (response.json()["version"], response.json()["content"]) == ("1", CONTENT)


def test_scr12_missing_version_returns_404_with_prd_message():
    client, _, _ = _app_client()

    response = client.get(f"/api/v1/store/scripts/{NAME}/9")

    assert response.status_code == 404
    assert response.json() == {"detail": f"Script não encontrado: {NAME}@9"}


def test_scr25_store_unavailable_returns_502_with_prd_message():
    client, registry, _ = _app_client()
    registry.error = AgentRegistryError(502, "AgentRegistry indisponível")

    response = client.get(f"/api/v1/store/scripts/{NAME}")

    assert response.status_code == 502
    assert response.json() == {"detail": "Store indisponível; tente de novo em instantes"}
