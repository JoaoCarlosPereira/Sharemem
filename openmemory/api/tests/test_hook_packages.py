"""Cobertura do pacote de Hook e da receita de instalação de hooks."""

import pytest

from app.services.hook_packages import (
    HookPackageInput,
    build_hook_archive,
    validate_command_references,
)
from app.services.store_recipes import (
    InstallRecipeService,
    InstallRecipeValidationError,
)

DESCRIPTION = "Bloqueia comandos perigosos no terminal da equipe antes da execução."


def hook_payload(**overrides):
    payload = {
        "name": "bloqueio-db",
        "tag": "latest",
        "title": "Bloqueio de banco externo",
        "description": DESCRIPTION,
        "events": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": "python ${HOOK_DIR}/guard.py"}],
                }
            ]
        },
        "files": [
            {"path": "HOOK.md", "content": "# Hook\n\n" + DESCRIPTION},
            {"path": "guard.py", "content": "import sys\nsys.exit(0)\n"},
        ],
    }
    payload.update(overrides)
    return HookPackageInput(**payload)


def test_pacote_exige_hook_md_na_raiz():
    payload = hook_payload(files=[{"path": "guard.py", "content": "print(1)"}])
    with pytest.raises(ValueError, match="HOOK.md"):
        build_hook_archive(payload)


def test_pacote_e_deterministico():
    primeiro, inventario = build_hook_archive(hook_payload())
    segundo, _ = build_hook_archive(hook_payload())
    assert primeiro == segundo
    assert [item["path"] for item in inventario] == ["HOOK.md", "guard.py"]


def test_comando_que_aponta_para_arquivo_ausente_e_rejeitado():
    payload = hook_payload(
        files=[{"path": "HOOK.md", "content": "# Hook\n\n" + DESCRIPTION}],
    )
    with pytest.raises(ValueError, match="guard.py"):
        validate_command_references(payload)


def test_entrada_de_comando_sem_command_e_rejeitada():
    with pytest.raises(ValueError):
        hook_payload(
            events={"PreToolUse": [{"hooks": [{"type": "command", "url": "http://x.invalid"}]}]}
        )


def test_evento_com_nome_invalido_e_rejeitado():
    with pytest.raises(ValueError, match="evento"):
        hook_payload(
            events={
                "Pre ToolUse": [
                    {"hooks": [{"type": "command", "command": "echo oi"}]}
                ]
            }
        )


def test_eventos_serializam_no_formato_do_registry():
    events = hook_payload().to_registry_events()
    assert events == {
        "PreToolUse": [
            {
                "hooks": [{"type": "command", "command": "python ${HOOK_DIR}/guard.py"}],
                "matcher": "Bash",
            }
        ]
    }


class FakeRegistryClient:
    def __init__(self, resource):
        self.resource = resource

    async def get_resource(self, *, kind, name, tag, auth_headers=None):
        return self.resource


def hook_resource(**spec_overrides):
    spec = {
        "title": "Bloqueio de banco externo",
        "description": DESCRIPTION,
        "events": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": "python ${HOOK_DIR}/guard.py"}],
                }
            ]
        },
    }
    spec.update(spec_overrides)
    return {
        "apiVersion": "ar.dev/v1alpha1",
        "kind": "Hook",
        "metadata": {"namespace": "default", "name": "bloqueio-db", "tag": "latest"},
        "spec": spec,
        "status": {
            "resolvedSource": {
                "artifact": {
                    "digest": "a" * 64,
                    "mediaType": "application/vnd.agentregistry.hook.v1.tar+gzip",
                    "size": 321,
                }
            }
        },
    }


@pytest.mark.asyncio
async def test_receita_extrai_pacote_e_faz_merge_no_settings():
    service = InstallRecipeService(registry_client=FakeRegistryClient(hook_resource()))
    recipe = await service.build(
        kind="hook",
        name="bloqueio-db",
        tag="latest",
        target="claude",
        user_id="S0293",
    )

    assert recipe["resource_kind"] == "hook"
    assert recipe["source"]["type"] == "registry_artifact"
    assert recipe["source"]["endpoint"].endswith("/hooks/bloqueio-db/latest/artifact")

    steps = {step["id"]: step for step in recipe["steps"]}
    assert steps["backup-settings"]["path"] == "~/.claude/settings.json"

    extract = steps["download-and-extract-hook-package"]
    assert extract["to"] == "~/.claude/hooks/bloqueio-db"
    assert extract["verify_artifact_sha256"] == "a" * 64

    merge = steps["merge-hook-events"]
    assert merge["type"] == "merge_hooks"
    assert merge["path"] == "~/.claude/settings.json"
    assert merge["content"]["hooks"]["PreToolUse"][0]["matcher"] == "Bash"
    assert merge["placeholders"]["HOOK_DIR"] == "~/.claude/hooks/bloqueio-db"
    assert merge["owner"] == {"kind": "hook", "name": "bloqueio-db"}

    verify_checks = steps["verify-hook-install"]["checks"]
    assert {"type": "config_key", "key": "hooks.PreToolUse"} in verify_checks

    rollback = {step["id"]: step for step in recipe["rollback"]}
    assert rollback["restore-backup"]["path"] == "~/.claude/settings.json"
    assert rollback["remove-hook-package"]["path"] == "~/.claude/hooks/bloqueio-db"


@pytest.mark.asyncio
async def test_hook_sem_eventos_nao_gera_receita():
    service = InstallRecipeService(registry_client=FakeRegistryClient(hook_resource(events={})))
    with pytest.raises(InstallRecipeValidationError, match="eventos"):
        await service.build(
            kind="hook", name="bloqueio-db", tag="latest", target="claude", user_id="S0293"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["cursor", "codex"])
async def test_alvos_sem_suporte_a_hook_falham_com_mensagem_clara(target):
    service = InstallRecipeService(registry_client=FakeRegistryClient(hook_resource()))
    with pytest.raises(InstallRecipeValidationError, match="não suporta"):
        await service.build(
            kind="hook", name="bloqueio-db", tag="latest", target=target, user_id="S0293"
        )
