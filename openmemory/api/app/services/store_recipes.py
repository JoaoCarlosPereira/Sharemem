"""Install recipe generation for the internal catalog store.

The service returns declarative, host-applied recipes. It never writes to the
server filesystem; every path in the payload is an allowlisted destination for
the selected editor target.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal, Optional, Protocol, TypedDict
from urllib.parse import quote

from app.utils.agentregistry import (
    KIND_TO_REGISTRY_COLLECTION,
    AgentRegistryError,
    AgentRegistryHttpClient,
    AgentRegistryResourceNotFound,
)

CatalogKind = Literal["skill", "hook", "mcpserver", "prompt", "agent", "plugin"]
InstallTarget = Literal["cursor", "claude", "codex"]

RECIPE_VERSION = "1"

SKILL_ARTIFACT_MEDIA_TYPE = "application/vnd.agentregistry.skill.v1.tar+gzip"
HOOK_ARTIFACT_MEDIA_TYPE = "application/vnd.agentregistry.hook.v1.tar+gzip"
PLUGIN_ARTIFACT_MEDIA_TYPE = "application/vnd.agentregistry.plugin.v1.tar+gzip"

# Placeholder a hook command uses to reach a file shipped in its own package.
# The host expands it to the directory the package was extracted into, which is
# only known at install time.
HOOK_DIR_PLACEHOLDER = "HOOK_DIR"

# Recipes are applied by hosts that only reach the public API, never the
# AgentRegistry backend directly, so the artifact endpoint must be the public
# passthrough in app/routers/store.py.
STORE_API_PREFIX = "/api/v1/store"

TARGET_DESTINATIONS: dict[str, dict[str, str]] = {
    "cursor": {
        "agent": ".cursor/agents/{name}.json",
        "mcpserver": ".cursor/mcp.json",
        "plugin": ".cursor/plugins/{name}",
        "prompt": ".cursor/prompts/{name}.md",
        "skill": ".cursor/skills/{name}",
    },
    "claude": {
        "agent": "~/.claude/agents/{name}.json",
        "hook": "~/.claude/settings.json",
        "mcpserver": ".mcp.json",
        "plugin": "~/.claude/plugins/{name}",
        "prompt": "~/.claude/prompts/{name}.md",
        "skill": "~/.claude/skills/{name}",
    },
    "codex": {
        "agent": "~/.codex/agents/{name}.json",
        "mcpserver": "~/.codex/config.toml",
        "plugin": "~/.codex/plugins/{name}",
        "prompt": "~/.codex/prompts/{name}.md",
        "skill": "~/.codex/skills/{name}",
    },
}

# Where a hook's package files land. Commands reach them through
# ${HOOK_DIR}, expanded to this path by the host.
HOOK_PACKAGE_DESTINATIONS: dict[str, str] = {
    "claude": "~/.claude/hooks/{name}",
}

# Claude Code (and peers) install plugins under a marketplace cache and then
# register/enable them via installed_plugins.json + settings.json.
DEFAULT_PLUGIN_MARKETPLACE = "sharemem"
PLUGIN_INSTALLED_JSON_VERSION = 2
PLUGIN_CACHE_DESTINATIONS: dict[str, str] = {
    "claude": "~/.claude/plugins/cache/{marketplace}/{name}/{version}",
    "cursor": ".cursor/plugins/cache/{marketplace}/{name}/{version}",
    "codex": "~/.codex/plugins/cache/{marketplace}/{name}/{version}",
}
PLUGIN_INSTALLED_PATHS: dict[str, str] = {
    "claude": "~/.claude/plugins/installed_plugins.json",
    "cursor": ".cursor/plugins/installed_plugins.json",
    "codex": "~/.codex/plugins/installed_plugins.json",
}
PLUGIN_SETTINGS_PATHS: dict[str, str] = {
    "claude": "~/.claude/settings.json",
    "cursor": ".cursor/settings.json",
    "codex": "~/.codex/settings.json",
}

_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class InstallRecipe(TypedDict):
    version: str
    resource_kind: str
    name: str
    tag: str
    target: str
    user_id: str
    resource: dict[str, Any]
    source: dict[str, Any]
    steps: list[dict[str, Any]]
    rollback: list[dict[str, Any]]


class RegistryClient(Protocol):
    async def get_resource(
        self,
        *,
        kind: str,
        name: str,
        tag: str,
        auth_headers: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        ...


class StoreRecipeError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class InstallRecipeValidationError(StoreRecipeError):
    def __init__(self, detail: str):
        super().__init__(400, detail)


class RegistryResourceNotFound(StoreRecipeError):
    def __init__(self, detail: str = "recurso não encontrado no AgentRegistry"):
        super().__init__(404, detail)


class InstallRecipeService:
    def __init__(self, registry_client: Optional[RegistryClient] = None):
        self.registry_client = registry_client or AgentRegistryHttpClient()

    async def build(
        self,
        *,
        kind: CatalogKind,
        name: str,
        tag: str,
        target: InstallTarget,
        user_id: str,
        auth_headers: Optional[dict[str, str]] = None,
    ) -> InstallRecipe:
        safe_kind = _validate_kind(kind)
        safe_target = _validate_target(target)
        safe_name = _validate_segment(name, "name")
        safe_tag = _validate_segment(tag, "tag")

        try:
            resource = await self.registry_client.get_resource(
                kind=safe_kind,
                name=safe_name,
                tag=safe_tag,
                auth_headers=auth_headers,
            )
        except AgentRegistryResourceNotFound as exc:
            raise RegistryResourceNotFound() from exc
        except AgentRegistryError as exc:
            raise StoreRecipeError(exc.status_code, exc.detail) from exc
        metadata = _metadata(resource)
        spec = _spec(resource)
        source = _resolve_source(safe_kind, resource, spec)
        destination = _destination(safe_kind, safe_target, safe_name)

        if safe_kind == "mcpserver":
            steps = _mcp_steps(safe_target, safe_name, destination, spec, source)
        elif safe_kind == "hook":
            steps = _hook_steps(safe_target, safe_name, destination, spec, source)
        elif safe_kind == "plugin":
            steps = _plugin_steps(safe_target, safe_name, resource, spec, source)
        else:
            steps = _file_steps(safe_kind, safe_name, destination, source, spec)

        if safe_kind == "hook":
            rollback = _rollback_steps(
                destination, _hook_package_destination(safe_target, safe_name)
            )
        elif safe_kind == "plugin":
            rollback = _rollback_steps(
                _plugin_installed_path(safe_target) or destination,
                _plugin_cache_destination(safe_target, safe_name, resource, spec),
            )
        else:
            rollback = _rollback_steps(destination)

        return {
            "version": RECIPE_VERSION,
            "resource_kind": safe_kind,
            "name": safe_name,
            "tag": safe_tag,
            "target": safe_target,
            "user_id": user_id,
            "resource": {
                "apiVersion": resource.get("apiVersion"),
                "kind": resource.get("kind") or _registry_kind_name(safe_kind),
                "metadata": {
                    "namespace": metadata.get("namespace") or "default",
                    "name": metadata.get("name") or safe_name,
                    "tag": metadata.get("tag") or safe_tag,
                    "labels": metadata.get("labels") or {},
                    "annotations": metadata.get("annotations") or {},
                },
                "title": spec.get("title") or metadata.get("name") or safe_name,
                "description": spec.get("description"),
            },
            "source": source,
            "steps": steps,
            "rollback": rollback,
        }


def _validate_kind(kind: str) -> str:
    if kind not in KIND_TO_REGISTRY_COLLECTION:
        raise InstallRecipeValidationError("kind inválido")
    return kind


def _validate_target(target: str) -> str:
    if target not in TARGET_DESTINATIONS:
        raise InstallRecipeValidationError("target inválido")
    return target


def _validate_segment(value: str, field_name: str) -> str:
    candidate = (value or "").strip()
    if not _SAFE_SEGMENT_RE.match(candidate):
        raise InstallRecipeValidationError(f"{field_name} contém caracteres inseguros")
    if ".." in candidate or "/" in candidate or "\\" in candidate or "\x00" in candidate:
        raise InstallRecipeValidationError(f"{field_name} não pode conter path traversal")
    return candidate


def _validate_relative_subpath(value: Optional[str], field_name: str = "subfolder") -> Optional[str]:
    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if candidate.startswith(("/", "~")) or "\\" in candidate or "\x00" in candidate:
        raise InstallRecipeValidationError(f"{field_name} deve ser relativo e seguro")
    parts = candidate.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise InstallRecipeValidationError(f"{field_name} contém path traversal")
    return candidate


def _metadata(resource: dict[str, Any]) -> dict[str, Any]:
    metadata = resource.get("metadata") or {}
    return metadata if isinstance(metadata, dict) else {}


def _spec(resource: dict[str, Any]) -> dict[str, Any]:
    spec = resource.get("spec") or {}
    return spec if isinstance(spec, dict) else {}


def _destination(kind: str, target: str, name: str) -> str:
    template = TARGET_DESTINATIONS[target].get(kind)
    if not template:
        raise InstallRecipeValidationError(
            f"o alvo {target} não suporta recursos do tipo {kind}"
        )
    return template.format(name=name)


def _registry_kind_name(kind: str) -> str:
    return {
        "agent": "Agent",
        "hook": "Hook",
        "mcpserver": "MCPServer",
        "plugin": "Plugin",
        "prompt": "Prompt",
        "skill": "Skill",
    }[kind]


CONTENT_ANNOTATION = "agentregistry.mem0.ai/skill-md"


def _resolve_source(kind: str, resource: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    source = spec.get("source") if isinstance(spec.get("source"), dict) else {}
    status = resource.get("status") if isinstance(resource.get("status"), dict) else {}
    resolved = status.get("resolvedSource") if isinstance(status.get("resolvedSource"), dict) else {}
    metadata = resource.get("metadata") if isinstance(resource.get("metadata"), dict) else {}
    annotations = metadata.get("annotations") if isinstance(metadata.get("annotations"), dict) else {}

    # Packaged skills/hooks/plugins are self-contained. Prefer the immutable
    # AgentRegistry artifact even if an older metadata record still carries a
    # Git repository for provenance.
    if kind in ("skill", "hook", "plugin"):
        resolved_artifact = resolved.get("artifact")
        if isinstance(resolved_artifact, dict) and resolved_artifact.get("digest"):
            default_media_type = {
                "skill": SKILL_ARTIFACT_MEDIA_TYPE,
                "hook": HOOK_ARTIFACT_MEDIA_TYPE,
                "plugin": PLUGIN_ARTIFACT_MEDIA_TYPE,
            }[kind]
            collection = {
                "skill": "skills",
                "hook": "hooks",
                "plugin": "plugins",
            }[kind]
            return {
                "type": "registry_artifact",
                "media_type": resolved_artifact.get("mediaType") or default_media_type,
                "artifact_digest": resolved_artifact.get("digest"),
                "size": resolved_artifact.get("size"),
                "endpoint": f"{STORE_API_PREFIX}/{collection}/{quote(str(metadata.get('name') or kind), safe='')}/{quote(str(metadata.get('tag') or 'latest'), safe='')}/artifact",
            }

    repository = _first_dict(
        source.get("repository"),
        (source.get("git") or {}).get("repository") if isinstance(source.get("git"), dict) else None,
        spec.get("repository"),
    )
    if repository:
        return _repository_source(repository, resolved)

    package = source.get("package") if isinstance(source.get("package"), dict) else None
    if package:
        return {"type": "package", "package": package}

    oci = source.get("oci") if isinstance(source.get("oci"), dict) else None
    if oci:
        reference = oci.get("reference")
        if not isinstance(reference, str) or not reference.strip():
            raise InstallRecipeValidationError("referência OCI inválida")
        return {"type": "oci", "reference": reference.strip(), "digest": resolved.get("digest")}

    remote = spec.get("remote") if isinstance(spec.get("remote"), dict) else None
    if remote:
        return {"type": "remote", "remote": remote}

    if kind == "prompt" and isinstance(spec.get("content"), str):
        return {"type": "inline", "filename": "prompt.md", "content": spec["content"]}

    if kind == "skill":
        skill_md = annotations.get(CONTENT_ANNOTATION)
        if isinstance(skill_md, str) and skill_md.strip():
            return {"type": "inline", "filename": "SKILL.md", "content": skill_md}
    return {
        "type": "registry",
        "note": "recurso sem fonte materializada; agente deve consultar o AgentRegistry",
    }


def _first_dict(*values: Any) -> Optional[dict[str, Any]]:
    for value in values:
        if isinstance(value, dict):
            return value
    return None


def _repository_source(repository: dict[str, Any], resolved: dict[str, Any]) -> dict[str, Any]:
    url = repository.get("url")
    if not isinstance(url, str) or not url.strip():
        raise InstallRecipeValidationError("repository.url ausente ou inválido")
    subfolder = _validate_relative_subpath(repository.get("subfolder"))
    return {
        "type": "git",
        "repository": url.strip(),
        "commit": repository.get("commit") or resolved.get("commit"),
        "branch": repository.get("branch"),
        "subfolder": subfolder,
    }


def _file_steps(
    kind: str,
    name: str,
    destination: str,
    source: dict[str, Any],
    spec: dict[str, Any],
) -> list[dict[str, Any]]:
    content = None
    if kind == "prompt" and isinstance(spec.get("content"), str):
        content = spec["content"]
    elif kind == "skill" and source.get("type") == "inline" and isinstance(source.get("content"), str):
        content = source["content"]
    copy_step: dict[str, Any] = {
        "id": "copy-resource",
        "type": "copy",
        "from": source,
        "to": destination,
        "overwrite": True,
        "idempotent": True,
    }
    if content is not None:
        copy_step["content"] = content

    if source.get("type") == "registry_artifact":
        copy_step = {
            "id": "download-and-extract-resource",
            "type": "download_and_extract",
            "from": source,
            "to": destination,
            "overwrite": True,
            "idempotent": True,
            "verify_artifact_sha256": source.get("artifact_digest"),
        }

    return [
        {
            "id": "backup-target",
            "type": "backup",
            "path": destination,
            "if_exists": True,
        },
        copy_step,
        {
            "id": "verify-target",
            "type": "verify",
            "path": destination,
            "checks": [{"type": "exists"}, {"type": "resource_name", "value": name}],
        },
    ]


def _hook_package_destination(target: str, name: str) -> Optional[str]:
    template = HOOK_PACKAGE_DESTINATIONS.get(target)
    return template.format(name=name) if template else None


def _hook_steps(
    target: str,
    name: str,
    destination: str,
    spec: dict[str, Any],
    source: dict[str, Any],
) -> list[dict[str, Any]]:
    """Install a hook: extract its package, then merge its events into settings.

    ``destination`` is the harness settings file. The package (scripts plus
    HOOK.md) is extracted next to it under HOOK_PACKAGE_DESTINATIONS, and the
    merge step carries the expansion for ${HOOK_DIR} so commands resolve to the
    files that were just extracted.
    """
    events = spec.get("events")
    if not isinstance(events, dict) or not events:
        raise InstallRecipeValidationError("hook sem eventos declarados")

    package_destination = _hook_package_destination(target, name)
    if not package_destination:
        raise InstallRecipeValidationError(f"o alvo {target} não suporta hooks")

    steps: list[dict[str, Any]] = [
        {
            "id": "backup-settings",
            "type": "backup",
            "path": destination,
            "if_exists": True,
        }
    ]

    if source.get("type") == "registry_artifact":
        steps.append(
            {
                "id": "download-and-extract-hook-package",
                "type": "download_and_extract",
                "description": (
                    "Baixe o artefato em from.endpoint, confira o sha256 e "
                    "extraia em to — é a pasta que ${HOOK_DIR} representa."
                ),
                "from": source,
                "to": package_destination,
                "overwrite": True,
                "idempotent": True,
                "verify_artifact_sha256": source.get("artifact_digest"),
            }
        )

    steps.append(
        {
            "id": "merge-hook-events",
            "type": "merge_hooks",
            # merge_hooks é um tipo de passo novo e quem aplica a receita é um
            # agente lendo este JSON — a descrição evita depender de convenção.
            "description": (
                "Para cada evento em content.hooks, some os grupos ao bloco "
                "hooks do arquivo em path, preservando o que já existe. "
                "Antes de escrever, troque ${HOOK_DIR} pelos valores de "
                "placeholders. Se já houver entrada idêntica deste mesmo hook, "
                "substitua no lugar em vez de acrescentar."
            ),
            "path": destination,
            # `key` names the settings block the events belong under, so a host
            # that only knows merge_json can still place them correctly.
            "key": "hooks",
            "content": {"hooks": events},
            # Entries are matched by (matcher, action) inside each event and
            # replaced rather than appended, so reinstalling the same tag twice
            # leaves one copy — never a duplicated hook that fires twice.
            "strategy": "replace_owned_entries",
            "owner": {"kind": "hook", "name": name},
            "placeholders": {HOOK_DIR_PLACEHOLDER: package_destination},
            "idempotent": True,
        }
    )

    steps.append(
        {
            "id": "verify-hook-install",
            "type": "verify",
            "path": destination,
            "checks": [
                {"type": "exists"},
                {"type": "config_key", "key": "hooks"},
                *[{"type": "config_key", "key": f"hooks.{event}"} for event in sorted(events)],
            ],
        }
    )
    return steps


def _plugin_manifest(resource: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    status = resource.get("status") if isinstance(resource.get("status"), dict) else {}
    for candidate in (spec.get("manifest"), status.get("manifest")):
        if isinstance(candidate, dict):
            return candidate
    return {}


def _plugin_marketplace(spec: dict[str, Any]) -> str:
    marketplace = spec.get("marketplace")
    if isinstance(marketplace, str) and marketplace.strip():
        return marketplace.strip()
    return DEFAULT_PLUGIN_MARKETPLACE


def _plugin_version(resource: dict[str, Any], spec: dict[str, Any]) -> str:
    manifest = _plugin_manifest(resource, spec)
    for candidate in (spec.get("version"), manifest.get("version")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    raise InstallRecipeValidationError("plugin sem version declarada")


def _plugin_cache_destination(
    target: str,
    name: str,
    resource: dict[str, Any],
    spec: dict[str, Any],
) -> Optional[str]:
    template = PLUGIN_CACHE_DESTINATIONS.get(target)
    if not template:
        return None
    return template.format(
        marketplace=_plugin_marketplace(spec),
        name=name,
        version=_plugin_version(resource, spec),
    )


def _plugin_installed_path(target: str) -> Optional[str]:
    return PLUGIN_INSTALLED_PATHS.get(target)


def _plugin_settings_path(target: str) -> Optional[str]:
    return PLUGIN_SETTINGS_PATHS.get(target)


def _plugin_steps(
    target: str,
    name: str,
    resource: dict[str, Any],
    spec: dict[str, Any],
    source: dict[str, Any],
) -> list[dict[str, Any]]:
    """Install a Claude-style plugin: cache files, register, enable, prompt config."""
    marketplace = _plugin_marketplace(spec)
    version = _plugin_version(resource, spec)
    plugin_key = f"{name}@{marketplace}"
    cache_destination = _plugin_cache_destination(target, name, resource, spec)
    installed_path = _plugin_installed_path(target)
    settings_path = _plugin_settings_path(target)
    if not cache_destination or not installed_path or not settings_path:
        raise InstallRecipeValidationError(f"o alvo {target} não suporta plugins")

    steps: list[dict[str, Any]] = []
    if source.get("type") == "registry_artifact":
        steps.append(
            {
                "id": "download-and-extract-plugin",
                "type": "download_and_extract",
                "from": source,
                "to": cache_destination,
                "overwrite": True,
                "idempotent": True,
                "verify_artifact_sha256": source.get("artifact_digest"),
            }
        )
    elif source.get("type") == "git":
        steps.append(
            {
                "id": "copy-plugin-from-git",
                "type": "copy",
                "from": source,
                "to": cache_destination,
                "overwrite": True,
                "idempotent": True,
            }
        )
    else:
        raise InstallRecipeValidationError(
            "plugin precisa de artefato no registry ou fonte git"
        )

    steps.append(
        {
            "id": "register-installed-plugin",
            "type": "merge_json",
            "path": installed_path,
            "strategy": "replace_key",
            "key": f"plugins.{plugin_key}",
            "content": {
                "version": PLUGIN_INSTALLED_JSON_VERSION,
                "plugins": {
                    plugin_key: {
                        "version": version,
                        "installPath": cache_destination,
                        "marketplace": marketplace,
                    }
                },
            },
            "idempotent": True,
        }
    )
    steps.append(
        {
            "id": "enable-plugin",
            "type": "merge_json",
            "path": settings_path,
            "strategy": "replace_key",
            "key": f"enabledPlugins.{plugin_key}",
            "content": {"enabledPlugins": {plugin_key: True}},
            "idempotent": True,
        }
    )

    manifest = _plugin_manifest(resource, spec)
    user_config = manifest.get("userConfig")
    if isinstance(user_config, dict):
        for key, cfg in user_config.items():
            if not isinstance(key, str) or not key.strip():
                continue
            config = cfg if isinstance(cfg, dict) else {}
            steps.append(
                {
                    "id": f"prompt-user-config-{key.strip()}",
                    "type": "prompt",
                    "key": key.strip(),
                    "sensitive": bool(config.get("sensitive")),
                    "config": config,
                }
            )
    return steps


def _mcp_steps(
    target: str,
    name: str,
    destination: str,
    spec: dict[str, Any],
    source: dict[str, Any],
) -> list[dict[str, Any]]:
    entry = _mcp_server_entry(name, spec, source)
    merge_step: dict[str, Any]
    if target == "codex":
        merge_step = {
            "id": "merge-mcp-config",
            "type": "merge_toml",
            "path": destination,
            "content": _codex_mcp_toml(name, entry),
            "strategy": "replace_key",
            "key": f"mcp_servers.{name}",
            "idempotent": True,
        }
    else:
        merge_step = {
            "id": "merge-mcp-config",
            "type": "merge_json",
            "path": destination,
            "content": {"mcpServers": {name: entry}},
            "strategy": "replace_key",
            "key": f"mcpServers.{name}",
            "idempotent": True,
        }
    return [
        {
            "id": "backup-config",
            "type": "backup",
            "path": destination,
            "if_exists": True,
        },
        merge_step,
        {
            "id": "verify-mcp-config",
            "type": "verify",
            "path": destination,
            "checks": [{"type": "config_key", "key": name}],
        },
    ]


def _mcp_server_entry(name: str, spec: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    remote = spec.get("remote") if isinstance(spec.get("remote"), dict) else None
    if remote and isinstance(remote.get("url"), str):
        entry = {
            "type": remote.get("type") or "http",
            "url": remote["url"],
        }
        headers = remote.get("headers")
        if headers:
            entry["headers"] = headers
        return entry

    if source.get("type") == "package":
        package = source.get("package") or {}
        identifier = package.get("identifier") or name
        version = package.get("version")
        package_ref = f"{identifier}@{version}" if version else identifier
        registry_type = str(package.get("registryType") or "").lower()
        if registry_type == "npm":
            return {
                "command": "npx",
                "args": ["-y", package_ref, *_argument_values(package.get("packageArguments"))],
            }
        return {
            "command": str(package.get("runtimeHint") or package.get("registryType") or "run"),
            "args": [package_ref, *_argument_values(package.get("packageArguments"))],
        }

    return {
        "type": "stdio",
        "resource": {"kind": "mcpserver", "name": name, "source": source},
    }


def _argument_values(arguments: Any) -> list[str]:
    if not isinstance(arguments, list):
        return []
    values: list[str] = []
    for argument in arguments:
        if not isinstance(argument, dict):
            continue
        value = argument.get("value")
        if isinstance(value, str) and value:
            values.append(value)
    return values


def _codex_mcp_toml(name: str, entry: dict[str, Any]) -> str:
    lines = [f"[mcp_servers.{name}]"]
    if entry.get("url"):
        lines.append(f"url = {json.dumps(entry['url'])}")
        if entry.get("type"):
            lines.append(f"type = {json.dumps(entry['type'])}")
    else:
        if entry.get("command"):
            lines.append(f"command = {json.dumps(entry['command'])}")
        if entry.get("args"):
            lines.append(f"args = {json.dumps(entry['args'])}")
    return "\n".join(lines) + "\n"


def _rollback_steps(
    destination: str,
    package_destination: Optional[str] = None,
) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = [
        {
            "id": "restore-backup",
            "type": "restore_backup",
            "path": destination,
            "if_backup_exists": True,
        }
    ]
    if package_destination:
        # The settings backup restores the event entries; the extracted package
        # has no backup to restore, so it is removed outright.
        steps.append(
            {
                "id": "remove-hook-package",
                "type": "remove",
                "path": package_destination,
                "if_exists": True,
            }
        )
    return steps
