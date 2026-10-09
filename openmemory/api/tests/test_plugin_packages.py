import io
import json
import tarfile

import pytest
from pydantic import ValidationError

from app.services.plugin_packages import (
    PluginPackageInput,
    PluginPackageMetadata,
    build_plugin_archive,
    validate_plugin_archive,
)
from app.services.store_recipes import InstallRecipeService


DESCRIPTION = "Plugin interno para compartilhar comandos e automações da equipe."


def manifest(**overrides):
    value = {
        "name": "demo-plugin",
        "version": "1.2.3",
        "description": DESCRIPTION,
        "userConfig": {"api_key": {"type": "string", "sensitive": True}},
    }
    value.update(overrides)
    return value


def payload(**overrides):
    doc = manifest()
    value = {
        "name": "demo-plugin",
        "tag": "latest",
        "version": "1.2.3",
        "marketplace": "sharemem",
        "title": "Plugin de demonstração",
        "description": DESCRIPTION,
        "manifest": doc,
        "files": [
            {"path": ".claude-plugin/plugin.json", "content": json.dumps(doc)},
            {"path": "README.md", "content": "# Plugin\n\nPlugin interno da equipe para automações."},
            {"path": "commands/status.md", "content": "# Status\n\nMostra informações para a equipe."},
            {"path": ".mcp.json", "content": '{"mcpServers":{"mem0":{"command":"uvx"}}}'},
        ],
    }
    value.update(overrides)
    return PluginPackageInput(**value)


def test_plugin_inline_materializa_artefato_e_componentes():
    archive, files, components = build_plugin_archive(payload())
    assert archive.startswith(b"\x1f\x8b")
    assert files[0]["path"] == ".claude-plugin/plugin.json"
    assert components["commands"] == ["/status"]
    assert components["mcpServers"] == ["mem0"]
    assert components["userConfig"] == ["api_key"]


@pytest.mark.parametrize(
    "changes,match",
    [
        ({"version": "latest"}, "semver"),
        ({"marketplace": "bad/name"}, "marketplace"),
        ({"manifest": manifest(name="outro")}, "manifest.name"),
        ({"manifest": manifest(version="2.0.0")}, "manifest.version"),
    ],
)
def test_identidade_invalida_e_rejeitada(changes, match):
    with pytest.raises(ValidationError, match=match):
        payload(**changes)


def test_readme_e_obrigatorio_e_deve_ser_pt_br():
    with pytest.raises(ValueError, match="README.md"):
        build_plugin_archive(payload(files=[payload().files[0]]))
    files = list(payload().files)
    files[1] = files[1].model_copy(update={"content": "# Plugin\n\nThis plugin executes useful internal automation for developers."})
    with pytest.raises(ValueError, match="PT-BR"):
        build_plugin_archive(payload(files=files))


@pytest.mark.parametrize("path", [".git/config", "node_modules/x.js", "__pycache__/x.pyc", ".in_use/1"])
def test_diretorios_de_cache_sao_rejeitados(path):
    files = list(payload().files) + [{"path": path, "content": "x"}]
    with pytest.raises(ValueError, match="proibido"):
        build_plugin_archive(payload(files=files))


def test_segredo_e_default_sensitive_sao_rejeitados():
    files = list(payload().files) + [{"path": "config.txt", "content": "ghp_abcdefghijklmnop"}]
    with pytest.raises(ValueError, match="segredo"):
        build_plugin_archive(payload(files=files))

    exposed = manifest(userConfig={"api_key": {"sensitive": True, "default": "secret"}})
    files = list(payload().files)
    files[0] = files[0].model_copy(update={"content": json.dumps(exposed)})
    with pytest.raises(ValueError, match="sensitive"):
        build_plugin_archive(payload(manifest=exposed, files=files))


def test_tarball_direto_aceita_plugin_valido():
    inline = payload()
    archive, expected_files, expected_components = build_plugin_archive(inline)
    metadata = PluginPackageMetadata(**inline.model_dump(exclude={"files"}))
    files, components = validate_plugin_archive(archive, metadata)
    assert files == expected_files
    assert components == expected_components


def test_tarball_direto_aceita_binario_maior_que_limite_de_skill():
    metadata = PluginPackageMetadata(**payload().model_dump(exclude={"files"}))
    files = {
        ".claude-plugin/plugin.json": json.dumps(metadata.manifest).encode(),
        "README.md": b"# Plugin\n\nPlugin interno da equipe para automacoes e testes.",
        "bin/plugin.exe": b"x" * ((4 << 20) + 1),
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path, content in files.items():
            info = tarfile.TarInfo(path)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))

    inventory, _ = validate_plugin_archive(buffer.getvalue(), metadata)
    assert next(item for item in inventory if item["path"] == "bin/plugin.exe")["size"] == (4 << 20) + 1


class FakeRegistryClient:
    async def get_resource(self, **_kwargs):
        doc = payload()
        return {
            "apiVersion": "ar.dev/v1alpha1",
            "kind": "Plugin",
            "metadata": {"name": doc.name, "tag": doc.tag, "namespace": "default"},
            "spec": {
                "title": doc.title,
                "description": doc.description,
                "version": doc.version,
                "marketplace": doc.marketplace,
                "manifest": doc.manifest,
                "source": {"type": "artifact"},
            },
            "status": {"resolvedSource": {"artifact": {
                "digest": "a" * 64,
                "mediaType": "application/vnd.agentregistry.plugin.v1.tar+gzip",
                "size": 123,
            }}},
        }


@pytest.mark.asyncio
async def test_receita_plugin_modela_gerenciador_do_claude():
    recipe = await InstallRecipeService(registry_client=FakeRegistryClient()).build(
        kind="plugin", name="demo-plugin", tag="latest", target="claude", user_id="user-1"
    )
    assert recipe["source"]["type"] == "registry_artifact"
    steps = {step["id"]: step for step in recipe["steps"]}
    assert steps["download-and-extract-plugin"]["to"].endswith("/sharemem/demo-plugin/1.2.3")
    assert steps["register-installed-plugin"]["type"] == "merge_json"
    assert steps["enable-plugin"]["content"]["enabledPlugins"]["demo-plugin@sharemem"] is True
    assert steps["prompt-user-config-api_key"]["sensitive"] is True
    assert recipe["rollback"][-1]["type"] == "remove"


class FakeGitRegistryClient:
    async def get_resource(self, **_kwargs):
        doc = payload()
        return {
            "apiVersion": "ar.dev/v1alpha1",
            "kind": "Plugin",
            "metadata": {"name": doc.name, "tag": doc.tag, "namespace": "default"},
            "spec": {
                "title": doc.title,
                "description": doc.description,
                "source": {
                    "type": "git",
                    "git": {
                        "repository": {
                            "url": "https://example.com/team/plugins.git",
                            "branch": "plugin/demo-1.2.3",
                            "subfolder": "plugins/demo-plugin",
                        }
                    },
                },
            },
            "status": {
                "manifest": doc.manifest,
                "resolvedSource": {"type": "git", "commit": "a" * 40},
            },
        }


@pytest.mark.asyncio
async def test_receita_plugin_git_usa_cache_e_manifesto_resolvido():
    recipe = await InstallRecipeService(registry_client=FakeGitRegistryClient()).build(
        kind="plugin", name="demo-plugin", tag="latest", target="claude", user_id="user-1"
    )

    steps = {step["id"]: step for step in recipe["steps"]}
    copy = steps["copy-plugin-from-git"]
    assert copy["from"]["commit"] == "a" * 40
    assert copy["to"] == "~/.claude/plugins/cache/sharemem/demo-plugin/1.2.3"
    assert steps["register-installed-plugin"]["content"]["version"] == 2
    assert steps["enable-plugin"]["content"]["enabledPlugins"]["demo-plugin@sharemem"] is True
