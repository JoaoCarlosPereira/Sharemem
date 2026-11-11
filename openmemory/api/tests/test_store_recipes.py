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



APP_DIR_WIN = "$HOME\\.sharemem\\apps\\horas-app"
APP_DIR_UNIX = "$HOME/.sharemem/apps/horas-app"
APP_WHEEL = "horas_app-0.1.0-py3-none-any.whl"
APP_ARTIFACT_DIGEST = "c" * 64
APP_WHEEL_SHA = "b" * 64


def app_resource(**spec_overrides):
    spec = {
        "title": "Controle de horas",
        "description": "Controle de horas local com lançamento no Redmine.",
        "usage": "Inicie com `horas-app` e confira com `horas-app verificar`.",
        "wheel": APP_WHEEL,
        "pythonRequires": ">=3.11",
        "commands": {"start": "horas-app", "verify": "horas-app verificar"},
        "settings": [
            {
                "name": "REDMINE_URL",
                "description": "Endereço do Redmine",
                "required": True,
                "secret": False,
                "env": "REDMINE_URL",
                "file": ".claude/apis/redmine.env",
                "key": "REDMINE_URL",
                "value": "https://redmine.exemplo.invalid",
            },
            {
                "name": "REDMINE_API_KEY",
                "description": "Sua chave de API do Redmine",
                "required": True,
                "secret": True,
                "env": "REDMINE_API_KEY",
                "file": ".claude/apis/redmine.env",
                "key": "REDMINE_API_KEY",
                "value": "segredo-do-planted-123",
            },
        ],
    }
    spec.update(spec_overrides)
    return {
        "apiVersion": "ar.dev/v1alpha1",
        "kind": "LocalApp",
        "metadata": {
            "namespace": "default",
            "name": "horas-app",
            "tag": "2",
            "annotations": {"sharemem.io/versao": "2", "sharemem.io/sha256": APP_WHEEL_SHA},
        },
        "spec": spec,
        "status": {
            "resolvedSource": {
                "artifact": {
                    "digest": APP_ARTIFACT_DIGEST,
                    "mediaType": "application/vnd.agentregistry.localapp.v1.tar+gzip",
                    "size": 4321,
                }
            }
        },
    }


async def _app_recipe(resource=None, target="claude"):
    registry = FakeRegistryClient(resource or app_resource())
    recipe = await InstallRecipeService(registry_client=registry).build(
        kind="app", name="horas-app", tag="2", target=target, user_id="user-1"
    )
    return recipe, registry


def _step(recipe, step_type):
    return next(step for step in recipe["steps"] if step["type"] == step_type)


@pytest.mark.asyncio
async def test_app02_recipe_has_the_seven_steps_in_techspec_order():
    recipe, registry = await _app_recipe()

    assert registry.calls[0]["kind"] == "app"
    assert recipe["resource_kind"] == "app"
    assert recipe["resource"]["kind"] == "LocalApp"
    assert [step["type"] for step in recipe["steps"]] == [
        "check_python",
        "download_and_extract",
        "write_file",
        "python_venv",
        "pip_install",
        "check_settings",
        "run_command",
        "verify",
    ]
    assert [step["id"] for step in recipe["steps"]] == [
        "check-python",
        "download-and-extract-app-package",
        "write-store-marker",
        "create-venv",
        "pip-install-wheel",
        "check-settings",
        "run-verify-command",
        "verify-app",
    ]


@pytest.mark.asyncio
async def test_app02_check_python_carries_requires_python_and_commands():
    recipe, _ = await _app_recipe()

    step = _step(recipe, "check_python")
    assert step["requires_python"] == ">=3.11"
    assert step["commands"] == {"windows": "python --version", "linux_macos": "python3 --version"}


@pytest.mark.asyncio
async def test_app02_download_step_has_artifact_sha_and_public_endpoint():
    recipe, _ = await _app_recipe()

    step = _step(recipe, "download_and_extract")
    assert step["verify_artifact_sha256"] == APP_ARTIFACT_DIGEST
    assert step["to"] == "~/.sharemem/apps/horas-app/pacote"
    assert step["overwrite"] is True
    assert step["from"]["endpoint"] == "/api/v1/store/apps/horas-app/2/artifact"
    assert step["from"]["type"] == "registry_artifact"
    assert step["from"]["media_type"] == "application/vnd.agentregistry.localapp.v1.tar+gzip"
    assert recipe["source"] == step["from"]


@pytest.mark.asyncio
async def test_app02_venv_and_pip_steps_have_ready_commands_for_both_systems():
    recipe, _ = await _app_recipe()

    venv = _step(recipe, "python_venv")
    assert venv["commands"] == {
        "windows": (
            f'if (-not (Test-Path "{APP_DIR_WIN}\\.venv\\Scripts\\python.exe")) '
            f'{{ python -m venv "{APP_DIR_WIN}\\.venv" }}'
        ),
        "linux_macos": (
            f'[ -x "{APP_DIR_UNIX}/.venv/bin/python" ] || python3 -m venv "{APP_DIR_UNIX}/.venv"'
        ),
    }
    pip = _step(recipe, "pip_install")
    assert pip["commands"] == {
        "windows": (
            f'& "{APP_DIR_WIN}\\.venv\\Scripts\\python.exe" -m pip install '
            f'--force-reinstall --no-cache-dir "{APP_DIR_WIN}\\pacote\\{APP_WHEEL}"'
        ),
        "linux_macos": (
            f'"{APP_DIR_UNIX}/.venv/bin/python" -m pip install '
            f'--force-reinstall --no-cache-dir "{APP_DIR_UNIX}/pacote/{APP_WHEEL}"'
        ),
    }


@pytest.mark.asyncio
async def test_app02_check_settings_lists_where_each_lives_without_any_value():
    recipe, _ = await _app_recipe()

    step = _step(recipe, "check_settings")
    assert step["settings"] == [
        {
            "name": "REDMINE_URL",
            "description": "Endereço do Redmine",
            "required": True,
            "secret": False,
            "env": "REDMINE_URL",
            "file": "~/.claude/apis/redmine.env",
            "key": "REDMINE_URL",
            "commands": _setting_commands("REDMINE_URL"),
        },
        {
            "name": "REDMINE_API_KEY",
            "description": "Sua chave de API do Redmine",
            "required": True,
            "secret": True,
            "env": "REDMINE_API_KEY",
            "file": "~/.claude/apis/redmine.env",
            "key": "REDMINE_API_KEY",
            "commands": _setting_commands("REDMINE_API_KEY"),
        },
    ]
    payload = json.dumps(recipe)
    assert "segredo-do-planted-123" not in payload
    assert "redmine.exemplo.invalid" not in payload


@pytest.mark.asyncio
async def test_app02_check_settings_omits_empty_locations_and_accepts_no_settings():
    only_env = app_resource(
        settings=[{"name": "TOKEN_X", "description": "d", "required": False, "secret": True, "env": "TOKEN_X"}]
    )
    recipe, _ = await _app_recipe(only_env)
    assert _step(recipe, "check_settings")["settings"] == [
        {
            "name": "TOKEN_X",
            "description": "d",
            "required": False,
            "secret": True,
            "env": "TOKEN_X",
            "commands": {"windows": "Test-Path env:TOKEN_X", "linux_macos": '[ -n "${TOKEN_X+x}" ]'},
        }
    ]

    recipe, _ = await _app_recipe(app_resource(settings=[]))
    assert _step(recipe, "check_settings")["settings"] == []


@pytest.mark.asyncio
async def test_app02_run_command_uses_the_venv_executable():
    recipe, _ = await _app_recipe()

    step = _step(recipe, "run_command")
    assert step["commands"] == {
        "windows": f'& "{APP_DIR_WIN}\\.venv\\Scripts\\horas-app.exe" verificar',
        "linux_macos": f'"{APP_DIR_UNIX}/.venv/bin/horas-app" verificar',
    }
    assert step["expect_exit_code"] == 0


@pytest.mark.asyncio
async def test_app02_final_verify_step_delivers_start_and_usage():
    recipe, _ = await _app_recipe()

    step = _step(recipe, "verify")
    assert step["start"] == {
        "windows": f'& "{APP_DIR_WIN}\\.venv\\Scripts\\horas-app.exe"',
        "linux_macos": f'"{APP_DIR_UNIX}/.venv/bin/horas-app"',
    }
    assert step["usage"] == "Inicie com `horas-app` e confira com `horas-app verificar`."


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["claude", "cursor", "codex"])
async def test_app02_marker_has_name_version_and_sha256_right_after_extract(target):
    recipe, _ = await _app_recipe(target=target)

    ids = [step["id"] for step in recipe["steps"]]
    assert ids.index("write-store-marker") == ids.index("download-and-extract-app-package") + 1
    marker_step = recipe["steps"][ids.index("write-store-marker")]
    assert marker_step["path"] == "~/.sharemem/apps/horas-app/.sharemem-store.json"
    assert json.loads(marker_step["content"]) == {
        "kind": "app",
        "name": "horas-app",
        "tag": "2",
        "digest": APP_ARTIFACT_DIGEST,
        "version": "2",
        "sha256": APP_WHEEL_SHA,
    }


@pytest.mark.asyncio
async def test_app02_recipe_has_no_rollback_steps():
    recipe, _ = await _app_recipe()

    assert recipe["rollback"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "spec_overrides",
    [
        {"commands": {"start": "horas-app", "verify": "horas-app verificar; rm -rf ~"}},
        {"commands": {"start": "../horas-app", "verify": "horas-app verificar"}},
        {"commands": {"start": "horas-app", "verify": "C:\\x\\horas-app.exe verificar"}},
        {"wheel": 'horas_app-0.1.0-py3-none-any.whl"; calc'},
        {"wheel": "horas_app.zip"},
        {"wheel": ""},
    ],
)
async def test_app02_unsafe_command_or_wheel_is_rejected(spec_overrides):
    with pytest.raises(InstallRecipeValidationError):
        await _app_recipe(app_resource(**spec_overrides))


@pytest.mark.asyncio
async def test_app02_app_without_published_artifact_is_rejected():
    resource = app_resource()
    del resource["status"]

    with pytest.raises(InstallRecipeValidationError) as exc_info:
        await _app_recipe(resource)

    assert exc_info.value.detail == "aplicativo sem artefato publicado"


def _setting_commands(name):
    return {
        "windows": (
            f"(Test-Path env:{name}) -or (Select-String -Quiet -Pattern '^{name}=' "
            f'-Path "$HOME\\.claude\\apis\\redmine.env")'
        ),
        "linux_macos": (
            f'[ -n "${{{name}+x}}" ] || grep -q \'^{name}=\' "$HOME/.claude/apis/redmine.env"'
        ),
    }


@pytest.mark.asyncio
async def test_app02_check_settings_in_registry_format_omits_false_flags_as_false():
    go_format = app_resource(
        settings=[
            {"name": "REDMINE_URL", "description": "d", "env": "REDMINE_URL"},
            {"name": "REDMINE_API_KEY", "description": "d", "secret": True, "env": "REDMINE_API_KEY"},
        ]
    )
    recipe, _ = await _app_recipe(go_format)

    settings = _step(recipe, "check_settings")["settings"]
    assert [(item["name"], item["required"], item["secret"]) for item in settings] == [
        ("REDMINE_URL", False, False),
        ("REDMINE_API_KEY", False, True),
    ]


@pytest.mark.asyncio
async def test_app02_check_settings_file_only_setting_has_file_command_without_env_clause():
    resource = app_resource(
        settings=[
            {"name": "K", "description": "d", "required": True, "secret": False, "file": "a/b.env", "key": "K"}
        ]
    )
    recipe, _ = await _app_recipe(resource)

    assert _step(recipe, "check_settings")["settings"] == [
        {
            "name": "K",
            "description": "d",
            "required": True,
            "secret": False,
            "key": "K",
            "file": "~/a/b.env",
            "commands": {
                "windows": "Select-String -Quiet -Pattern '^K=' -Path \"$HOME\\a\\b.env\"",
                "linux_macos": "grep -q '^K=' \"$HOME/a/b.env\"",
            },
        }
    ]


@pytest.mark.asyncio
async def test_app02_check_settings_skips_entries_that_are_not_dicts_or_have_no_name():
    resource = app_resource(
        settings=[
            "texto",
            None,
            {"description": "sem nome", "env": "X"},
            {"name": 7, "env": "X"},
            {"name": "OK", "description": "d", "required": True, "secret": False, "env": "OK"},
        ]
    )
    recipe, _ = await _app_recipe(resource)

    assert [item["name"] for item in _step(recipe, "check_settings")["settings"]] == ["OK"]


@pytest.mark.asyncio
async def test_app02_check_settings_setting_without_description_omits_the_field():
    resource = app_resource(settings=[{"name": "OK", "required": True, "secret": False, "env": "OK"}])
    recipe, _ = await _app_recipe(resource)

    assert _step(recipe, "check_settings")["settings"] == [
        {
            "name": "OK",
            "required": True,
            "secret": False,
            "env": "OK",
            "commands": {"windows": "Test-Path env:OK", "linux_macos": '[ -n "${OK+x}" ]'},
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "setting",
    [
        {"name": "A B", "env": "X"},
        {"name": "A;rm", "env": "X"},
        {"name": "OK", "env": "X; calc"},
        {"name": "OK", "env": "$(x)"},
        {"name": "OK", "env": "X", "key": "K'; calc; '"},
        {"name": "OK", "env": ["X"]},
        {"name": "OK", "env": ""},
        {"name": "OK", "file": "../x.env", "key": "K"},
        {"name": "OK", "file": "a/../x.env", "key": "K"},
        {"name": "OK", "file": "a/./x.env", "key": "K"},
        {"name": "OK", "file": "/etc/x.env", "key": "K"},
        {"name": "OK", "file": "a b/x.env", "key": "K"},
        {"name": "OK", "file": 'a"; calc; ".env', "key": "K"},
        {"name": "OK", "file": 7, "key": "K"},
    ],
)
async def test_app02_unsafe_setting_name_env_key_or_file_is_rejected(setting):
    with pytest.raises(InstallRecipeValidationError):
        await _app_recipe(app_resource(settings=[{"description": "d", "required": True, "secret": False, **setting}]))


@pytest.mark.asyncio
async def test_app02_download_endpoint_uses_immutable_version_even_when_tag_is_latest():
    resource = app_resource()
    resource["metadata"]["tag"] = "latest"
    registry = FakeRegistryClient(resource)

    recipe = await InstallRecipeService(registry_client=registry).build(
        kind="app", name="horas-app", tag="latest", target="claude", user_id="user-1"
    )

    assert recipe["source"]["endpoint"] == "/api/v1/store/apps/horas-app/2/artifact"
    assert _step(recipe, "download_and_extract")["from"]["endpoint"] == "/api/v1/store/apps/horas-app/2/artifact"
    marker = json.loads(_step(recipe, "write_file")["content"])
    assert (marker["tag"], marker["version"]) == ("latest", "2")


@pytest.mark.asyncio
async def test_app02_download_endpoint_falls_back_to_tag_without_version_annotation():
    resource = app_resource()
    resource["metadata"]["annotations"] = {}

    recipe, _ = await _app_recipe(resource)

    assert recipe["source"]["endpoint"] == "/api/v1/store/apps/horas-app/2/artifact"


@pytest.mark.asyncio
async def test_app02_artifact_without_media_type_uses_the_localapp_default():
    resource = app_resource()
    del resource["status"]["resolvedSource"]["artifact"]["mediaType"]

    recipe, _ = await _app_recipe(resource)

    assert recipe["source"]["media_type"] == "application/vnd.agentregistry.localapp.v1.tar+gzip"


@pytest.mark.asyncio
async def test_app02_marker_description_does_not_promise_auto_update_for_app():
    recipe, _ = await _app_recipe()

    description = _step(recipe, "write_file")["description"]
    assert "atualizador-store" not in description
    assert "nome, versão e sha256" in description
