import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.middleware.team_auth import AuthMiddleware
from app.services.store_recipes import (
    InstallRecipeService,
    InstallRecipeValidationError,
    RegistryResourceNotFound,
)

_PATH = Path(__file__).resolve().parents[1] / "app" / "routers" / "store.py"
_spec = importlib.util.spec_from_file_location("store_router_under_test", _PATH)
_store_router = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _store_router
_spec.loader.exec_module(_store_router)
store_router = _store_router.router
get_install_recipe_service = _store_router.get_install_recipe_service
MAX_UPDATE_PACKAGES = _store_router.MAX_UPDATE_PACKAGES


class FakeRegistryClient:
    def __init__(self, resource):
        self.resource = resource
        self.calls = []

    async def get_resource(self, *, kind, name, tag, auth_headers=None):
        self.calls.append(
            {
                "kind": kind,
                "name": name,
                "tag": tag,
                "auth_headers": auth_headers,
            }
        )
        return self.resource


def skill_resource(**repository_overrides):
    repository = {
        "url": "https://example.invalid/team/skills.git",
        "commit": "abc123",
        "subfolder": "skills/team-skill",
    }
    repository.update(repository_overrides)
    return {
        "apiVersion": "ar.dev/v1alpha1",
        "kind": "Skill",
        "metadata": {"namespace": "default", "name": "team-skill", "tag": "v1"},
        "spec": {
            "title": "Team Skill",
            "description": "Reusable team skill",
            "source": {"repository": repository},
        },
    }


def mcp_resource():
    return {
        "apiVersion": "ar.dev/v1alpha1",
        "kind": "MCPServer",
        "metadata": {"namespace": "default", "name": "ctx7", "tag": "v1"},
        "spec": {
            "title": "Context7",
            "source": {
                "package": {
                    "registryType": "npm",
                    "identifier": "@upstash/context7-mcp",
                    "version": "1.2.3",
                }
            },
        },
    }


def packaged_skill_resource():
    resource = skill_resource()
    resource["status"] = {
        "resolvedSource": {
            "artifact": {
                "digest": "a" * 64,
                "mediaType": "application/vnd.agentregistry.skill.v1.tar+gzip",
                "size": 1234,
            }
        }
    }
    return resource


@pytest.mark.asyncio
async def test_cursor_skill_recipe_uses_cursor_skill_path_and_rollback():
    registry = FakeRegistryClient(skill_resource())
    service = InstallRecipeService(registry_client=registry)

    recipe = await service.build(
        kind="skill",
        name="team-skill",
        tag="v1",
        target="cursor",
        user_id="user-1",
    )

    assert recipe["target"] == "cursor"
    assert recipe["steps"][0]["type"] == "backup"
    assert recipe["steps"][1]["type"] == "copy"
    assert recipe["steps"][1]["to"] == ".cursor/skills/team-skill"
    assert recipe["source"]["subfolder"] == "skills/team-skill"
    assert recipe["rollback"] == [
        {
            "id": "restore-backup",
            "type": "restore_backup",
            "path": ".cursor/skills/team-skill",
            "if_backup_exists": True,
        }
    ]


@pytest.mark.asyncio
async def test_packaged_skill_recipe_prefers_complete_artifact_over_legacy_git():
    registry = FakeRegistryClient(packaged_skill_resource())
    recipe = await InstallRecipeService(registry_client=registry).build(
        kind="skill", name="team-skill", tag="v1", target="cursor", user_id="user-1"
    )

    assert recipe["source"]["type"] == "registry_artifact"
    assert recipe["steps"][1]["type"] == "download_and_extract"
    assert recipe["steps"][1]["verify_artifact_sha256"] == "a" * 64


@pytest.mark.asyncio
async def test_packaged_skill_recipe_writes_store_marker_after_extract_for_claude():
    registry = FakeRegistryClient(packaged_skill_resource())
    recipe = await InstallRecipeService(registry_client=registry).build(
        kind="skill", name="team-skill", tag="v1", target="claude", user_id="user-1"
    )

    ids = [step["id"] for step in recipe["steps"]]
    assert ids.index("write-store-marker") == ids.index("download-and-extract-resource") + 1
    marker_step = recipe["steps"][ids.index("write-store-marker")]
    assert marker_step["path"] == "~/.claude/skills/team-skill/.sharemem-store.json"
    assert json.loads(marker_step["content"]) == {
        "kind": "skill", "name": "team-skill", "tag": "v1", "digest": "a" * 64,
    }


@pytest.mark.asyncio
async def test_store_marker_is_only_for_claude_packages():
    registry = FakeRegistryClient(packaged_skill_resource())
    cursor = await InstallRecipeService(registry_client=registry).build(
        kind="skill", name="team-skill", tag="v1", target="cursor", user_id="user-1"
    )
    legacy = await InstallRecipeService(registry_client=FakeRegistryClient(skill_resource())).build(
        kind="skill", name="team-skill", tag="v1", target="claude", user_id="user-1"
    )

    assert all(step["id"] != "write-store-marker" for step in cursor["steps"])
    assert all(step["id"] != "write-store-marker" for step in legacy["steps"])


@pytest.mark.asyncio
async def test_packaged_skill_recipe_points_at_public_artifact_passthrough():
    """The endpoint must be reachable by hosts and must not be the ZIP route.

    ``/download`` re-zips the package server-side, so its bytes hash differently
    from the published digest and would fail ``verify_artifact_sha256``.
    """
    registry = FakeRegistryClient(packaged_skill_resource())
    recipe = await InstallRecipeService(registry_client=registry).build(
        kind="skill", name="team-skill", tag="v1", target="claude", user_id="user-1"
    )

    endpoint = recipe["source"]["endpoint"]
    assert endpoint == "/api/v1/store/skills/team-skill/v1/artifact"
    assert not endpoint.startswith("/v0/")
    assert not endpoint.endswith("/download")
    assert recipe["steps"][1]["from"]["endpoint"] == endpoint


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "expected_path"),
    [
        ("claude", "~/.claude/skills/team-skill"),
        ("codex", "~/.codex/skills/team-skill"),
    ],
)
async def test_claude_and_codex_skill_targets_are_covered(target, expected_path):
    registry = FakeRegistryClient(skill_resource())
    service = InstallRecipeService(registry_client=registry)

    recipe = await service.build(
        kind="skill",
        name="team-skill",
        tag="v1",
        target=target,
        user_id="user-1",
    )

    assert recipe["steps"][1]["to"] == expected_path
    assert recipe["steps"][2]["type"] == "verify"


@pytest.mark.asyncio
async def test_mcpserver_cursor_recipe_merges_cursor_json_config():
    registry = FakeRegistryClient(mcp_resource())
    service = InstallRecipeService(registry_client=registry)

    recipe = await service.build(
        kind="mcpserver",
        name="ctx7",
        tag="v1",
        target="cursor",
        user_id="user-1",
    )

    merge = recipe["steps"][1]
    assert merge["type"] == "merge_json"
    assert merge["path"] == ".cursor/mcp.json"
    assert merge["content"]["mcpServers"]["ctx7"]["command"] == "npx"
    assert (
        "@upstash/context7-mcp@1.2.3"
        in merge["content"]["mcpServers"]["ctx7"]["args"]
    )


@pytest.mark.asyncio
async def test_mcpserver_codex_recipe_merges_codex_toml_config():
    registry = FakeRegistryClient(mcp_resource())
    service = InstallRecipeService(registry_client=registry)

    recipe = await service.build(
        kind="mcpserver",
        name="ctx7",
        tag="v1",
        target="codex",
        user_id="user-1",
    )

    merge = recipe["steps"][1]
    assert merge["type"] == "merge_toml"
    assert merge["path"] == "~/.codex/config.toml"
    assert "[mcp_servers.ctx7]" in merge["content"]


@pytest.mark.asyncio
async def test_name_path_traversal_is_rejected_before_registry_call():
    registry = FakeRegistryClient(skill_resource())
    service = InstallRecipeService(registry_client=registry)

    with pytest.raises(InstallRecipeValidationError):
        await service.build(
            kind="skill",
            name="../team-skill",
            tag="v1",
            target="cursor",
            user_id="user-1",
        )

    assert registry.calls == []


@pytest.mark.asyncio
async def test_repository_subfolder_path_traversal_is_rejected():
    registry = FakeRegistryClient(skill_resource(subfolder="../outside"))
    service = InstallRecipeService(registry_client=registry)

    with pytest.raises(InstallRecipeValidationError):
        await service.build(
            kind="skill",
            name="team-skill",
            tag="v1",
            target="cursor",
            user_id="user-1",
        )


class FakeRecipeService:
    async def build(self, **kwargs):
        return {
            "version": "1",
            "resource_kind": kwargs["kind"],
            "name": kwargs["name"],
            "tag": kwargs["tag"],
            "target": kwargs["target"],
            "user_id": kwargs["user_id"],
            "resource": {},
            "source": {"type": "registry"},
            "steps": [{"id": "verify-target", "type": "verify", "path": ".cursor/skills/demo"}],
            "rollback": [{"id": "restore-backup", "type": "restore_backup", "path": ".cursor/skills/demo"}],
        }


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "enforce")
    app = FastAPI()
    app.add_middleware(AuthMiddleware, mode="enforce", token_to_team={"team-token": "dev"})
    app.include_router(store_router)
    app.dependency_overrides[get_install_recipe_service] = lambda: FakeRecipeService()
    return app


@pytest.mark.asyncio
async def test_install_recipe_endpoint_returns_steps_and_rollback(app):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": "team-token"},
    ) as client:
        response = await client.post(
            "/api/v1/store/install-recipes",
            json={"kind": "skill", "name": "demo", "tag": "v1", "target": "cursor"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == "team:dev"
    assert body["steps"][0]["type"] == "verify"
    assert body["rollback"][0]["type"] == "restore_backup"


@pytest.mark.asyncio
async def test_install_recipe_endpoint_rejects_invalid_kind_with_422(app):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": "team-token"},
    ) as client:
        response = await client.post(
            "/api/v1/store/install-recipes",
            json={"kind": "model", "name": "demo", "tag": "v1", "target": "cursor"},
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_install_recipe_endpoint_without_auth_is_denied(app):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/store/install-recipes",
            json={"kind": "skill", "name": "demo", "tag": "v1", "target": "cursor"},
        )

    assert response.status_code == 401


class FakeUpdatesService(FakeRecipeService):
    async def build(self, **kwargs):
        if kwargs["name"] == "ausente":
            raise RegistryResourceNotFound()
        if kwargs["name"] == "invalido":
            raise InstallRecipeValidationError("nome inválido")
        recipe = await super().build(**kwargs)
        recipe["source"] = {"type": "registry_artifact", "artifact_digest": "a" * 64}
        return recipe


@pytest.mark.asyncio
async def test_updates_endpoint_answers_every_package_in_one_call(app):
    app.dependency_overrides[get_install_recipe_service] = lambda: FakeUpdatesService()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": "team-token"},
    ) as client:
        response = await client.post(
            "/api/v1/store/updates",
            json={"packages": [
                {"kind": "hook", "name": "recall"},
                {"kind": "skill", "name": "ausente", "tag": "v2"},
                {"kind": "skill", "name": "invalido"},
            ]},
        )

    assert response.status_code == 200
    recall, ausente, invalido = response.json()["packages"]
    assert recall["found"] is True
    assert recall["recipe"]["target"] == "claude"
    assert recall["recipe"]["source"]["artifact_digest"] == "a" * 64
    assert ausente == {"kind": "skill", "name": "ausente", "tag": "v2", "found": False}
    assert invalido["found"] is False and invalido["error"] == "nome inválido"


@pytest.mark.asyncio
async def test_updates_endpoint_limits_batch_size_and_kinds(app):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": "team-token"},
    ) as client:
        grande = await client.post(
            "/api/v1/store/updates",
            json={"packages": [{"kind": "skill", "name": f"s{i}"} for i in range(MAX_UPDATE_PACKAGES + 1)]},
        )
        plugin = await client.post(
            "/api/v1/store/updates",
            json={"packages": [{"kind": "plugin", "name": "p"}]},
        )

    assert grande.status_code == 422
    assert plugin.status_code == 422

