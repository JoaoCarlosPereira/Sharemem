import json
import os

os.environ.setdefault("OPENAI_API_KEY", "test-key")

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app import mcp_server
from app.utils.agentregistry import AgentRegistryResourceNotFound

MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


def _skill_resource(name="team-skill", tag="v1"):
    return {
        "apiVersion": "ar.dev/v1alpha1",
        "kind": "Skill",
        "metadata": {
            "namespace": "default",
            "name": name,
            "tag": tag,
            "labels": {"team": "platform"},
        },
        "spec": {
            "title": "Team Skill",
            "description": "Reusable team skill",
            "source": {"repository": {"url": "https://example.invalid/repo.git"}},
        },
    }


class FakeCatalogClient:
    def __init__(self):
        self.calls = []
        self.items = [_skill_resource("team-skill"), _skill_resource("other-skill")]
        self.get_error = None

    async def list_resources(self, **kwargs):
        self.calls.append(("list", kwargs))
        return {"items": self.items, "nextCursor": ""}

    async def get_resource(self, **kwargs):
        self.calls.append(("get", kwargs))
        if self.get_error:
            raise self.get_error
        return _skill_resource(kwargs["name"], kwargs.get("tag") or "latest")

    async def apply_resource(self, **kwargs):
        self.calls.append(("apply", kwargs))
        return {
            "results": [
                {
                    "apiVersion": kwargs["resource"].get("apiVersion"),
                    "kind": kwargs["resource"].get("kind"),
                    "name": kwargs["resource"]["metadata"]["name"],
                    "status": "created",
                }
            ]
        }


class FakeRecipeService:
    def __init__(self):
        self.calls = []

    async def build(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "version": "1",
            "resource_kind": kwargs["kind"],
            "name": kwargs["name"],
            "tag": kwargs["tag"],
            "target": kwargs["target"],
            "user_id": kwargs["user_id"],
            "resource": {},
            "source": {"type": "registry"},
            "steps": [{"id": "verify-target", "type": "verify"}],
            "rollback": [],
        }


@pytest.mark.asyncio
async def test_search_catalog_accepts_empty_query_and_honors_limit(monkeypatch):
    fake = FakeCatalogClient()
    monkeypatch.setattr(mcp_server, "get_agent_registry_client", lambda: fake)

    out = await mcp_server.search_catalog(query="", kind="skill", limit=1)
    body = json.loads(out)

    assert len(body["results"]) == 1
    assert body["results"][0]["name"] == "team-skill"
    assert fake.calls[0][1]["limit"] == 1


def _script_resource(name="gcenfe02-xml"):
    return {
        "apiVersion": "ar.dev/v1alpha1",
        "kind": "Script",
        "metadata": {
            "namespace": "default",
            "name": name,
            "tag": "latest",
            "annotations": {"sharemem.io/autor": "pessoa-1", "sharemem.io/versao": "3"},
        },
        "spec": {
            "description": "Decodifica a coluna GCENFE02.XML",
            "usage": "python gcenfe02_xml.py <arquivo>",
            "fileName": "gcenfe02_xml.py",
            "language": "python",
            "effects": ["somente-leitura"],
            "area": "sysmo-s1",
            "tags": ["nfe"],
            "content": "print(1)\n",
        },
    }


@pytest.mark.asyncio
async def test_scr08_search_without_kind_also_lists_scripts(monkeypatch):
    fake = FakeCatalogClient()
    monkeypatch.setattr(mcp_server, "get_agent_registry_client", lambda: fake)

    await mcp_server.search_catalog(query="nada-casa-isto")

    listed = [call[1]["kind"] for call in fake.calls]
    assert "script" in listed
    script_call = next(call[1] for call in fake.calls if call[1]["kind"] == "script")
    assert script_call["latest_only"] is True
    skill_call = next(call[1] for call in fake.calls if call[1]["kind"] == "skill")
    assert skill_call["latest_only"] is False


@pytest.mark.asyncio
async def test_scr07_search_script_kind_returns_card_with_latest_only(monkeypatch):
    fake = FakeCatalogClient()
    fake.items = [_script_resource()]
    monkeypatch.setattr(mcp_server, "get_agent_registry_client", lambda: fake)

    out = await mcp_server.search_catalog(query="XML GCENFE02", kind="script")

    assert fake.calls[0][1]["latest_only"] is True
    result = json.loads(out)["results"][0]
    assert result["name"] == "gcenfe02-xml"
    assert result["language"] == "python"
    assert result["effects"] == ["somente-leitura"]
    assert result["area"] == "sysmo-s1"
    assert result["tags"] == ["nfe"]
    assert result["author"] == "pessoa-1"
    assert result["version"] == "3"


@pytest.mark.asyncio
@pytest.mark.parametrize("requested,sent", [(500, 100), (None, 20)])
async def test_scr09_search_limit_is_clamped(monkeypatch, requested, sent):
    fake = FakeCatalogClient()
    monkeypatch.setattr(mcp_server, "get_agent_registry_client", lambda: fake)
    kwargs = {"limit": requested} if requested is not None else {}

    out = await mcp_server.search_catalog(query="", kind="script", **kwargs)

    assert fake.calls[0][1]["limit"] == sent
    assert json.loads(out)["limit"] == sent


def test_scr07_search_catalog_description_mentions_script():
    description = mcp_server.mcp._tool_manager.get_tool("search_catalog").description
    assert "script" in description


def _script_view(version="2", content="print(2)\n"):
    from app.services.script_catalog import ScriptCard, ScriptVersion, ScriptView

    return ScriptView(
        name="gcenfe02-xml",
        version=version,
        is_latest=True,
        file_name="gcenfe02_xml.py",
        content=content,
        sha256="abc",
        warning=None,
        card=ScriptCard(
            title=None,
            description="Decodifica a coluna GCENFE02.XML",
            usage="python gcenfe02_xml.py <arquivo>",
            language="python",
            requirements=[],
            effects=["somente-leitura"],
            area="sysmo-s1",
            tags=[],
            author="pessoa-1",
            published_at="2026-10-27T00:00:00+00:00",
        ),
        versions=[ScriptVersion(version=version, author="pessoa-1", published_at=None, is_latest=True)],
    )


class FakeScriptCatalog:
    instances = []

    def __init__(self, client, db, auth_headers):
        self.client = client
        self.db = db
        self.auth_headers = auth_headers
        self.calls = []
        FakeScriptCatalog.instances.append(self)

    async def publish(self, script, author):
        from app.services.script_catalog import PublishResult

        self.calls.append(("publish", script, author))
        return PublishResult(name=script.name, version="1", sha256="abc", created=True)

    async def get(self, name, version=None):
        self.calls.append(("get", name, version))
        return _script_view(version or "2")

    async def rollback(self, name, version, author):
        self.calls.append(("rollback", name, version, author))
        return _script_view(version, "print(1)\n")


@pytest.fixture
def script_catalog(monkeypatch):
    from app.services import script_catalog as catalog_module

    FakeScriptCatalog.instances = []
    fake_client = FakeCatalogClient()
    monkeypatch.setattr(catalog_module, "ScriptCatalog", FakeScriptCatalog)
    monkeypatch.setattr(mcp_server, "get_agent_registry_client", lambda: fake_client)
    token_auth = mcp_server.registry_auth_headers_var.set({"Authorization": "Bearer omtk_token"})
    token_user = mcp_server.auth_user_var.set("person-1")
    yield FakeScriptCatalog
    mcp_server.auth_user_var.reset(token_user)
    mcp_server.registry_auth_headers_var.reset(token_auth)


def _publish_args(**overrides):
    args = {
        "name": "gcenfe02-xml",
        "file_name": "gcenfe02_xml.py",
        "description": "Decodifica a coluna GCENFE02.XML",
        "usage": "python gcenfe02_xml.py <arquivo>",
        "language": "python",
        "effects": ["somente-leitura"],
        "area": "sysmo-s1",
        "content": "print(1)\n",
        "confirm_user_requested": True,
    }
    args.update(overrides)
    return args


@pytest.mark.asyncio
async def test_scr01_publish_script_without_confirmation_does_not_call_service(script_catalog):
    out = await mcp_server.publish_script(**_publish_args(confirm_user_requested=False))

    assert out.startswith("Error: ")
    assert "confirm_user_requested=true" in out
    assert script_catalog.instances == []


@pytest.mark.asyncio
async def test_scr01_publish_script_returns_version_and_sha_with_mcp_actor(script_catalog):
    out = await mcp_server.publish_script(**_publish_args(tags=["nfe"]))

    assert json.loads(out) == {"name": "gcenfe02-xml", "version": "1", "sha256": "abc", "created": True}
    service = script_catalog.instances[0]
    _, script, author = service.calls[0]
    assert author == "person-1"
    assert script.name == "gcenfe02-xml"
    assert script.tags == ["nfe"]
    assert script.requirements == []
    assert service.auth_headers == {"Authorization": "Bearer omtk_token"}


@pytest.mark.asyncio
async def test_publish_script_rule_error_becomes_error_message(script_catalog):
    out = await mcp_server.publish_script(**_publish_args(name="A"))

    assert out == "Error: Nome inválido"
    assert script_catalog.instances == []


@pytest.mark.asyncio
async def test_scr10_get_script_returns_script_view_json(script_catalog):
    out = await mcp_server.get_script(name="gcenfe02-xml")

    assert json.loads(out) == _script_view().model_dump()
    assert script_catalog.instances[0].calls == [("get", "gcenfe02-xml", None)]


@pytest.mark.asyncio
async def test_get_script_translated_error_becomes_error_message(script_catalog, monkeypatch):
    from app.services.script_packages import ScriptRuleError

    async def missing(self, name, version=None):
        raise ScriptRuleError("Script não encontrado: nao-existe@9")

    monkeypatch.setattr(FakeScriptCatalog, "get", missing)

    out = await mcp_server.get_script(name="nao-existe", version="9")

    assert out == "Error: Script não encontrado: nao-existe@9"


@pytest.mark.asyncio
async def test_get_script_unexpected_error_is_logged_and_returned(script_catalog, monkeypatch, caplog):
    async def broken(self, name, version=None):
        raise RuntimeError("falha inesperada")

    monkeypatch.setattr(FakeScriptCatalog, "get", broken)

    out = await mcp_server.get_script(name="gcenfe02-xml")

    assert out == "Error: falha inesperada"
    assert any(record.levelname == "ERROR" for record in caplog.records)


@pytest.mark.asyncio
async def test_scr29_rollback_script_requires_confirmation(script_catalog):
    out = await mcp_server.rollback_script(name="gcenfe02-xml", version="1")

    assert out.startswith("Error: ")
    assert "confirm_user_requested=true" in out
    assert script_catalog.instances == []


@pytest.mark.asyncio
async def test_scr29_rollback_script_returns_new_latest(script_catalog):
    out = await mcp_server.rollback_script(
        name="gcenfe02-xml", version="1", confirm_user_requested=True
    )

    assert json.loads(out) == _script_view("1", "print(1)\n").model_dump()
    assert script_catalog.instances[0].calls == [("rollback", "gcenfe02-xml", "1", "person-1")]


@pytest.mark.asyncio
async def test_get_catalog_resource_returns_error_on_missing_resource(monkeypatch):
    fake = FakeCatalogClient()
    fake.get_error = AgentRegistryResourceNotFound()
    monkeypatch.setattr(mcp_server, "get_agent_registry_client", lambda: fake)

    out = await mcp_server.get_catalog_resource(kind="skill", name="missing", tag="v1")

    assert out == "Error: recurso não encontrado no AgentRegistry"


@pytest.mark.asyncio
async def test_publish_catalog_resource_forwards_apply_payload_and_auth(monkeypatch):
    fake = FakeCatalogClient()
    monkeypatch.setattr(mcp_server, "get_agent_registry_client", lambda: fake)
    token = mcp_server.registry_auth_headers_var.set({"Authorization": "Bearer omtk_token"})
    try:
        out = await mcp_server.publish_catalog_resource(
            resource=_skill_resource("published-skill"),
            dry_run=True,
            confirm_user_requested=True,
        )
    finally:
        mcp_server.registry_auth_headers_var.reset(token)

    body = json.loads(out)
    assert body["result"]["results"][0]["status"] == "created"
    call = fake.calls[0][1]
    assert call["resource"]["metadata"]["name"] == "published-skill"
    assert call["dry_run"] is True
    assert call["auth_headers"] == {"Authorization": "Bearer omtk_token"}


@pytest.mark.asyncio
async def test_publish_catalog_resource_synthesizes_legacy_auth_when_absent(monkeypatch):
    """Hostname-only MCP (same as memories) must still authenticate to the loja."""
    fake = FakeCatalogClient()
    monkeypatch.setattr(mcp_server, "get_agent_registry_client", lambda: fake)
    monkeypatch.setenv("MEM0_AUTH_ALLOW_LEGACY", "1")
    method = mcp_server.auth_method_var.set("legacy")
    try:
        out = await mcp_server.publish_catalog_resource(
            resource=_skill_resource("legacy-skill"),
            dry_run=True,
            confirm_user_requested=True,
        )
    finally:
        mcp_server.auth_method_var.reset(method)

    body = json.loads(out)
    assert body["result"]["results"][0]["status"] == "created"
    assert fake.calls[0][1]["auth_headers"] == {"Authorization": "Bearer local"}


@pytest.mark.asyncio
async def test_get_install_recipe_delegates_to_service_with_mcp_actor_and_auth(monkeypatch):
    service = FakeRecipeService()
    monkeypatch.setattr(mcp_server, "get_catalog_install_recipe_service", lambda: service)
    token_auth = mcp_server.registry_auth_headers_var.set({"Authorization": "Bearer omtk_token"})
    token_method = mcp_server.auth_method_var.set("agent_token")
    token_user = mcp_server.auth_user_var.set("person-1")
    try:
        out = await mcp_server.get_install_recipe(
            kind="skill",
            name="team-skill",
            tag="v1",
            target="cursor",
            confirm_user_requested=True,
        )
    finally:
        mcp_server.auth_user_var.reset(token_user)
        mcp_server.auth_method_var.reset(token_method)
        mcp_server.registry_auth_headers_var.reset(token_auth)

    body = json.loads(out)
    assert body["recipe"]["target"] == "cursor"
    assert service.calls == [
        {
            "kind": "skill",
            "name": "team-skill",
            "tag": "v1",
            "target": "cursor",
            "user_id": "person-1",
            "auth_headers": {"Authorization": "Bearer omtk_token"},
        }
    ]


@pytest.mark.asyncio
async def test_authenticated_mcp_tool_call_forwards_authorization_header(monkeypatch):
    fake = FakeCatalogClient()
    monkeypatch.setattr(mcp_server, "get_agent_registry_client", lambda: fake)

    app = FastAPI()
    mcp_server.setup_mcp_server(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post(
            "/mcp/cursor/http/Mini-PC",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test-client", "version": "0.1.0"},
                },
            },
            headers={**MCP_HEADERS, "Authorization": "Bearer omtk_token"},
        )
        response = await client.post(
            "/mcp/cursor/http/Mini-PC",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "search_catalog",
                    "arguments": {"query": "team", "kind": "skill", "limit": 5},
                },
            },
            headers={**MCP_HEADERS, "Authorization": "Bearer omtk_token"},
        )

    assert response.status_code == 200
    payload = response.json()
    result_text = payload["result"]["structuredContent"]["result"]
    body = json.loads(result_text)
    assert body["results"][0]["name"] == "team-skill"
    assert fake.calls[-1][1]["auth_headers"] == {"Authorization": "Bearer omtk_token"}
