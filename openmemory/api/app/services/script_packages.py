from __future__ import annotations

import hashlib
import os
import re
from typing import Any, Literal

from pydantic import BaseModel, field_validator, model_validator

Effect = Literal["somente-leitura", "grava-arquivos", "altera-banco", "acessa-rede"]

REQUIRED_FIELDS = ("description", "usage", "language", "effects", "area")
ALLOWED_EXTENSIONS = (".py", ".sql", ".ps1", ".sh", ".bat", ".cmd", ".js", ".mjs")
SENSITIVE_EFFECTS = ("altera-banco", "grava-arquivos")
MAX_CONTENT_BYTES = 256 * 1024
MAX_DESCRIPTION_CHARS = 4000
NAME_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
NAME_LENGTH = range(2, 65)
CARD_KEYS = (
    "title",
    "description",
    "usage",
    "fileName",
    "language",
    "requirements",
    "effects",
    "area",
    "tags",
)

_REFERENCE_START = r"\"'$%{"
CREDENTIAL_PATTERNS = (
    re.compile(r"\bomtk_\w+"),
    re.compile(rf"PGPASSWORD\s*=(?!=)\s*[\"']?(?![{_REFERENCE_START}\s]|$)"),
    re.compile(
        rf"\bpassword\s*=(?!=)\s*(?:[\"'](?![{_REFERENCE_START}])|(?<==)[^\s{_REFERENCE_START}(),;]+(?=[\s;\"']|$))",
        re.IGNORECASE,
    ),
)


class ScriptRuleError(Exception):
    pass


class ScriptInput(BaseModel):
    name: str
    file_name: str
    title: str | None = None
    description: str
    usage: str
    language: str
    requirements: list[str] = []
    effects: list[Effect]
    area: str
    tags: list[str] = []
    content: str

    @model_validator(mode="before")
    @classmethod
    def require_card_fields(cls, data: Any) -> Any:
        for field in REQUIRED_FIELDS:
            value = data.get(field)
            if not (value.strip() if isinstance(value, str) else value):
                raise ScriptRuleError(f"Campo obrigatório ausente: {field}")
        return data

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if len(value) not in NAME_LENGTH or not NAME_PATTERN.match(value):
            raise ScriptRuleError("Nome inválido")
        return value

    @field_validator("file_name")
    @classmethod
    def validate_extension(cls, value: str) -> str:
        extension = os.path.splitext(value)[1]
        if extension not in ALLOWED_EXTENSIONS:
            raise ScriptRuleError(f"Extensão não aceita: {extension}")
        return value

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        if len(value) > MAX_DESCRIPTION_CHARS:
            raise ScriptRuleError("Descrição acima de 4.000 caracteres")
        return value

    @field_validator("content", mode="before")
    @classmethod
    def validate_content(cls, value: Any) -> Any:
        try:
            raw = value if isinstance(value, bytes) else str(value).encode("utf-8")
            text = raw.decode("utf-8")
        except UnicodeError as exc:
            raise ScriptRuleError("Script precisa ser texto UTF-8") from exc
        if len(raw) > MAX_CONTENT_BYTES:
            raise ScriptRuleError("Script acima de 256 KB")
        line = find_credential_line(text)
        if line is not None:
            raise ScriptRuleError(f"Script contém credencial na linha {line}")
        return text


def find_credential_line(content: str) -> int | None:
    for number, line in enumerate(content.splitlines(), start=1):
        if any(pattern.search(line) for pattern in CREDENTIAL_PATTERNS):
            return number
    return None


def effect_warning(effects: list[str]) -> str | None:
    sensitive = [effect for effect in effects if effect in SENSITIVE_EFFECTS]
    if not sensitive:
        return None
    return f"Este script {', '.join(sensitive)}; confirme com o usuário antes de rodar"


def content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def next_version(tags: list[str]) -> str:
    return str(max((int(tag) for tag in tags if tag.isdigit()), default=0) + 1)


def is_same_publication(script: ScriptInput, published_spec: dict, published_sha256: str | None) -> bool:
    if published_sha256 != content_sha256(script.content):
        return False
    new_spec = to_registry_resource(script, "", {})["spec"]
    return _comparable_card(published_spec) == _comparable_card(new_spec)


def _comparable_card(spec: dict) -> dict:
    return {key: spec.get(key) or None for key in CARD_KEYS}


def to_registry_resource(script: ScriptInput, tag: str, annotations: dict[str, str]) -> dict:
    optional = {"title": script.title, "requirements": script.requirements, "tags": script.tags}
    spec = {
        "description": script.description,
        "usage": script.usage,
        "fileName": script.file_name,
        "language": script.language,
        "effects": list(script.effects),
        "area": script.area,
        "content": script.content,
    }
    return {
        "apiVersion": "ar.dev/v1alpha1",
        "kind": "Script",
        "metadata": {"name": script.name, "tag": tag, "annotations": annotations},
        "spec": {**{key: value for key, value in optional.items() if value}, **spec},
    }
