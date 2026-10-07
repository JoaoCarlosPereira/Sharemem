import io
import json
import tarfile

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.store import get_registry_client, router
from app.services.store_recipes import PLUGIN_ARTIFACT_MEDIA_TYPE


MANIFEST = {"name": "demo-plugin", "version": "1.2.3"}
METADATA = {
    "name": "demo-plugin",
    "tag": "latest",
    "version": "1.2.3",
    "marketplace": "sharemem",
    "title": "Plugin de demonstração",
    "description": "Plugin interno para compartilhar automações da equipe.",
    "manifest": MANIFEST,
}


class FakeRegistryClient:
    def __init__(self):
        self.resource = None
        self.archive = None

    async def apply_resource(self, *, resource, auth_headers=None):
        self.resource = resource
        return {"results": [{"status": "created"}]}

    async def put_package_artifact(self, *, kind, name, tag, archive, auth_headers=None):
        assert kind == "plugin"
        self.archive = archive
        return {"status_code": 201}

    async def list_resources(self, **_kwargs):
        return {"items": [self.resource] if self.resource else []}


def app_client():
    app = FastAPI()
    app.include_router(router)
    registry = FakeRegistryClient()
    app.dependency_overrides[get_registry_client] = lambda: registry
    return TestClient(app), registry


def inline_payload():
    return {
        **METADATA,
        "files": [
            {"path": ".claude-plugin/plugin.json", "content": json.dumps(MANIFEST)},
            {"path": "README.md", "content": "# Plugin\n\nPlugin interno criado para a equipe."},
        ],
    }


def tar_payload():
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for path, content in {
            ".claude-plugin/plugin.json": json.dumps(MANIFEST).encode(),
            "README.md": b"# Plugin\n\nPlugin interno criado para a equipe.",
        }.items():
            info = tarfile.TarInfo(path)
            info.size = len(content)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(content))
    return out.getvalue()


def test_put_plugin_json_inline_publica_componentes_e_artefato():
    client, registry = app_client()
    response = client.put(
        "/api/v1/store/plugins/demo-plugin/latest",
        headers={"Authorization": "Bearer local"},
        json=inline_payload(),
    )
    assert response.status_code == 200, response.text
    assert registry.resource["spec"]["source"] == {"type": "artifact"}
    assert registry.resource["spec"]["version"] == "1.2.3"
    assert registry.archive.startswith(b"\x1f\x8b")


def test_put_plugin_tarball_direto_publica_sem_base64():
    client, registry = app_client()
    # Reusa o mesmo buffer: gzip embute mtime no header, então duas chamadas
    # a tar_payload() no mesmo segundo-limite falham em assert de igualdade.
    archive = tar_payload()
    response = client.put(
        "/api/v1/store/plugins/demo-plugin/latest",
        headers={
            "Authorization": "Bearer local",
            "Content-Type": PLUGIN_ARTIFACT_MEDIA_TYPE,
            "X-Plugin-Metadata": json.dumps(METADATA),
        },
        content=archive,
    )
    assert response.status_code == 200, response.text
    assert registry.archive == archive


def test_marketplace_agrega_plugin_empacotado():
    client, registry = app_client()
    publish = client.put(
        "/api/v1/store/plugins/demo-plugin/latest",
        headers={"Authorization": "Bearer local"},
        json=inline_payload(),
    )
    assert publish.status_code == 200
    response = client.get("/api/v1/store/marketplace.json")
    assert response.status_code == 200
    plugin = response.json()["plugins"][0]
    assert plugin["name"] == "demo-plugin"
    assert plugin["version"] == "1.2.3"
    assert plugin["source"]["source"] == "url"
