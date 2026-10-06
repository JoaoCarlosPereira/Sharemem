"""Validation, indexing and deterministic packaging for Store Plugins."""

from __future__ import annotations

import io
import json
import re
import tarfile
from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.services.skill_packages import (
    SkillFileInput,
    _build_archive,
    _decode_file,
    _validate_pt_br,
)

PLUGIN_MANIFEST_PATH = ".claude-plugin/plugin.json"
PLUGIN_README_PATH = "README.md"
MAX_INLINE_PLUGIN_FILES = 256
MAX_PLUGIN_FILES = 10_000
MAX_PLUGIN_ARCHIVE_BYTES = 64 << 20
MAX_PLUGIN_UNCOMPRESSED_BYTES = 128 << 20
MAX_PLUGIN_FILE_BYTES = 64 << 20
SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
MARKETPLACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
FORBIDDEN_PARTS = {".git", "node_modules", "__pycache__", ".in_use"}
SECRET_PATTERNS = {
    "chave Mem0": re.compile(rb"m0-[A-Za-z0-9_-]{8,}"),
    "token ShareMem": re.compile(rb"omtk_[A-Za-z0-9_-]{8,}"),
    "token GitLab": re.compile(rb"glpat-[A-Za-z0-9_-]{8,}"),
    "token GitHub": re.compile(rb"ghp_[A-Za-z0-9_-]{8,}"),
    "chave AWS": re.compile(rb"AKIA[0-9A-Z]{16}"),
}


class PluginPackageMetadata(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    tag: str = Field(default="latest", min_length=1, max_length=128)
    version: str
    marketplace: str = Field(min_length=1, max_length=128)
    title: str | None = Field(default=None, max_length=240)
    description: str = Field(min_length=1, max_length=4000)
    language: str = "pt-BR"
    harnesses: list[str] = Field(default_factory=lambda: ["claude-code"], max_length=16)
    manifest: dict[str, Any]

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        if not SEMVER_RE.fullmatch(value):
            raise ValueError("version deve ser semver (ex.: 1.2.3)")
        return value

    @field_validator("marketplace")
    @classmethod
    def validate_marketplace(cls, value: str) -> str:
        if not MARKETPLACE_RE.fullmatch(value):
            raise ValueError("marketplace contém caracteres inválidos")
        return value

    @field_validator("language")
    @classmethod
    def validate_language(cls, value: str) -> str:
        if value != "pt-BR":
            raise ValueError("Plugins da Store devem declarar language=pt-BR")
        return value

    @model_validator(mode="after")
    def validate_manifest_identity(self) -> "PluginPackageMetadata":
        if self.manifest.get("name") != self.name:
            raise ValueError("manifest.name não confere com name")
        if self.manifest.get("version") != self.version:
            raise ValueError("manifest.version não confere com version")
        return self


class PluginPackageInput(PluginPackageMetadata):
    files: list[SkillFileInput] = Field(min_length=1, max_length=MAX_INLINE_PLUGIN_FILES)


def build_plugin_archive(
    payload: PluginPackageInput,
) -> tuple[bytes, list[dict[str, Any]], dict[str, list[str]]]:
    """Validate and pack the JSON-inline Plugin form."""
    file_map = {file.path: _decode_file(file) for file in payload.files}
    _validate_plugin_files(payload, file_map)
    archive, inventory = _build_archive(
        payload.files,
        required_root_file=PLUGIN_MANIFEST_PATH,
        label="Plugin",
    )
    return archive, inventory, derive_components(file_map, payload.manifest)


def validate_plugin_archive(
    archive: bytes,
    metadata: PluginPackageMetadata,
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """Validate a direct tar.gz upload without expanding it onto disk."""
    if not archive or len(archive) > MAX_PLUGIN_ARCHIVE_BYTES:
        raise ValueError(f"artefato deve ter no máximo {MAX_PLUGIN_ARCHIVE_BYTES} bytes")
    files: dict[str, bytes] = {}
    modes: dict[str, int] = {}
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            for member in tar:
                if member.isdir():
                    continue
                if not member.isfile():
                    raise ValueError(f"entrada especial não permitida: {member.name}")
                _validate_plugin_path(member.name)
                if member.name in files:
                    raise ValueError(f"arquivo duplicado: {member.name}")
                if len(files) >= MAX_PLUGIN_FILES:
                    raise ValueError(f"plugin excede {MAX_PLUGIN_FILES} arquivos")
                if member.size < 0 or member.size > MAX_PLUGIN_FILE_BYTES:
                    raise ValueError(f"arquivo excede {MAX_PLUGIN_FILE_BYTES} bytes: {member.name}")
                total += member.size
                if total > MAX_PLUGIN_UNCOMPRESSED_BYTES:
                    raise ValueError("conteúdo descompactado excede o limite da Store")
                extracted = tar.extractfile(member)
                if extracted is None:
                    raise ValueError(f"não foi possível ler {member.name}")
                files[member.name] = extracted.read(MAX_PLUGIN_FILE_BYTES + 1)
                modes[member.name] = member.mode
    except (tarfile.TarError, OSError) as exc:
        raise ValueError("artefato do Plugin não é um tar.gz válido") from exc
    _validate_plugin_files(metadata, files)
    inventory = [
        {"path": path, "size": len(content), "mode": modes[path]}
        for path, content in sorted(files.items())
    ]
    return inventory, derive_components(files, metadata.manifest)


def _validate_plugin_files(metadata: PluginPackageMetadata, files: dict[str, bytes]) -> None:
    for path, content in files.items():
        _validate_plugin_path(path)
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(content):
                raise ValueError(f"segredo detectado em {path}: {label}")
    for required in (PLUGIN_MANIFEST_PATH, PLUGIN_README_PATH):
        if required not in files:
            raise ValueError(f"{required} é obrigatório no Plugin")
    try:
        file_manifest = json.loads(files[PLUGIN_MANIFEST_PATH].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{PLUGIN_MANIFEST_PATH} deve conter JSON válido") from exc
    if file_manifest != metadata.manifest:
        raise ValueError("manifest não confere com .claude-plugin/plugin.json")
    try:
        _validate_pt_br(files[PLUGIN_README_PATH].decode("utf-8"), PLUGIN_README_PATH)
    except UnicodeDecodeError as exc:
        raise ValueError("README.md deve ser UTF-8") from exc
    _validate_sensitive_defaults(metadata.manifest)
    if "hooks/hooks.json" in files:
        _validate_plugin_hooks(files["hooks/hooks.json"])


def _validate_plugin_path(path: str) -> None:
    candidate = PurePosixPath(path)
    if not path or path.startswith("/") or "\\" in path or str(candidate) != path:
        raise ValueError(f"caminho inválido: {path}")
    if any(part in FORBIDDEN_PARTS or part in ("", ".", "..") for part in candidate.parts):
        raise ValueError(f"diretório proibido no Plugin: {path}")


def _validate_sensitive_defaults(manifest: dict[str, Any]) -> None:
    config = manifest.get("userConfig")
    if not isinstance(config, dict):
        return
    for name, schema in config.items():
        if not isinstance(schema, dict) or schema.get("sensitive") is not True:
            continue
        if any(schema.get(field) not in (None, "") for field in ("value", "default")):
            raise ValueError(f"userConfig sensitive não pode trazer valor: {name}")


def _validate_plugin_hooks(raw: bytes) -> None:
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("hooks/hooks.json deve conter JSON válido") from exc
    events = document.get("hooks") if isinstance(document, dict) else None
    if not isinstance(events, dict) or not events:
        raise ValueError("hooks/hooks.json deve declarar hooks")
    for event, groups in events.items():
        if not isinstance(groups, list) or not groups:
            raise ValueError(f"hook sem grupos: {event}")
        for group in groups:
            entries = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(entries, list) or not entries:
                raise ValueError(f"grupo sem hooks: {event}")
            for entry in entries:
                command = entry.get("command") if isinstance(entry, dict) else None
                if not command:
                    continue
                if command.startswith(("/", "~/")):
                    raise ValueError(f"hook usa caminho absoluto: {event}")
                if "${CLAUDE_PLUGIN_ROOT}" not in command:
                    raise ValueError(f"hook command deve usar ${{CLAUDE_PLUGIN_ROOT}}: {event}")


def derive_components(files: dict[str, bytes], manifest: dict[str, Any]) -> dict[str, list[str]]:
    components: dict[str, list[str]] = {
        "hooks": [], "commands": [], "agents": [], "skills": [], "mcpServers": [], "userConfig": [],
    }
    for path in files:
        if path.startswith("commands/") and path.endswith(".md"):
            components["commands"].append("/" + PurePosixPath(path).stem)
        elif path.startswith("agents/") and path.endswith(".md"):
            components["agents"].append(PurePosixPath(path).stem)
        elif path.startswith("skills/") and path.endswith("/SKILL.md"):
            components["skills"].append(path.split("/")[1])
    if "hooks/hooks.json" in files:
        hooks = json.loads(files["hooks/hooks.json"].decode("utf-8")).get("hooks", {})
        components["hooks"] = sorted(hooks)
    if ".mcp.json" in files:
        mcp = json.loads(files[".mcp.json"].decode("utf-8"))
        servers = mcp.get("mcpServers") if isinstance(mcp, dict) else None
        if isinstance(servers, dict):
            components["mcpServers"] = sorted(servers)
    user_config = manifest.get("userConfig")
    if isinstance(user_config, dict):
        components["userConfig"] = sorted(user_config)
    return {key: sorted(set(values)) for key, values in components.items()}
