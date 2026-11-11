from __future__ import annotations

import base64
import hashlib
import io
import re
import zipfile
from email.message import Message
from email.parser import HeaderParser
from typing import Any

from pydantic import BaseModel, field_validator, model_validator

from app.services.script_packages import (
    Effect,
    ScriptRuleError,
    check_description,
    check_name,
    find_credential_line,
)
from app.services.skill_packages import MAX_FILE_BYTES, SkillFileInput, _build_archive

APP_ROOT_FILE = "APP.md"
REQUIRED_FIELDS = ("description", "usage", "effects", "area")
REQUIRED_METADATA = ("Name", "Version", "Requires-Python")
MAX_METADATA_BYTES = 1 << 20
COMMAND_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(?: [A-Za-z0-9][A-Za-z0-9._-]*)*")
WHEEL_FILE_PATTERN = re.compile(r"[A-Za-z0-9_.]+-[A-Za-z0-9_.!+]+(?:-[0-9][A-Za-z0-9_.]*)?(?:-[A-Za-z0-9_.]+){3}\.whl")
METADATA_PATTERN = re.compile(r"[^/]+\.dist-info/METADATA")
IDENTIFIER_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
HOME_FILE_PATTERN = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*")


class AppRuleError(ScriptRuleError):
    pass


class AppCommands(BaseModel):
    start: str
    verify: str


class AppSetting(BaseModel):
    name: str
    description: str
    required: bool
    secret: bool
    env: str | None = None
    file: str | None = None
    key: str | None = None

    @model_validator(mode="before")
    @classmethod
    def require_flags(cls, data: Any) -> Any:
        if isinstance(data, dict):
            name = data.get("name")
            if not (isinstance(name, str) and name.strip()):
                raise AppRuleError("Campo obrigatório ausente: settings.name")
            for flag in ("required", "secret"):
                if flag not in data or data[flag] is None:
                    raise AppRuleError(f"Campo obrigatório ausente: settings.{name}.{flag}")
                if not isinstance(data[flag], bool):
                    raise AppRuleError(f"settings.{name}.{flag} deve ser true ou false")
        return data

    @model_validator(mode="after")
    def validate_setting(self) -> AppSetting:
        for field in ("name", "env", "key"):
            value = getattr(self, field)
            if value is not None and not IDENTIFIER_PATTERN.fullmatch(value):
                raise AppRuleError(f"Configuração {self.name!r}: {field} deve ser um identificador")
        if self.file is not None and not is_home_file(self.file):
            raise AppRuleError(f"Configuração {self.name}: file deve ser relativo a ~, sem ..")
        if not _single_line(self.description):
            raise AppRuleError(f"Configuração {self.name}: descrição deve ter uma linha só")
        if bool(self.file) != bool(self.key):
            raise AppRuleError(f"Configuração {self.name}: file e key vão juntos")
        if not self.env and not self.file:
            raise AppRuleError(f"Configuração {self.name} precisa de env ou de file + key")
        return self


class AppInput(BaseModel):
    name: str
    title: str | None = None
    description: str
    usage: str
    requirements: list[str] = []
    effects: list[Effect]
    area: str
    tags: list[str] = []
    commands: AppCommands
    settings: list[AppSetting] = []
    wheel_name: str
    wheel: bytes
    python_requires: str

    @classmethod
    def from_upload(cls, card: dict[str, Any], wheel_name: str, wheel: bytes) -> AppInput:
        _require_card_fields(card)
        metadata = _read_wheel_metadata(wheel_name, wheel)
        app = cls(
            **{key: value for key, value in card.items() if key in _CARD_FIELDS},
            wheel_name=wheel_name,
            wheel=wheel,
            python_requires=metadata["Requires-Python"],
        )
        _require_same_name(app.name, metadata["Name"])
        _require_same_name(app.name, wheel_name.split("-", 1)[0])
        return app

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return check_name(value, AppRuleError)

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        return check_description(value, AppRuleError)

    @model_validator(mode="after")
    def validate_card(self) -> AppInput:
        for field in ("start", "verify"):
            if not COMMAND_PATTERN.fullmatch(getattr(self.commands, field)):
                raise AppRuleError(f"commands.{field} deve ser um comando do app, sem caminho nem operador")
        for field, text in [("title", self.title or ""), *[("requirements", item) for item in self.requirements]]:
            if not _single_line(text):
                raise AppRuleError(f"{field} deve ter uma linha só")
        for field, text in _card_texts(self):
            if find_credential_line(text) is not None:
                raise AppRuleError(f"Ficha contém credencial em {field}")
        return self


_CARD_FIELDS = set(AppInput.model_fields) - {"wheel_name", "wheel", "python_requires"}


def _require_card_fields(card: dict[str, Any]) -> None:
    for field in REQUIRED_FIELDS:
        value = card.get(field)
        if not (value.strip() if isinstance(value, str) else value):
            raise AppRuleError(f"Campo obrigatório ausente: {field}")
    commands = card.get("commands")
    if not isinstance(commands, dict):
        commands = {}
    for field in ("start", "verify"):
        if not str(commands.get(field) or "").strip():
            raise AppRuleError(f"Campo obrigatório ausente: commands.{field}")


def _read_wheel_metadata(wheel_name: str, wheel: bytes) -> Message:
    if not WHEEL_FILE_PATTERN.fullmatch(wheel_name):
        raise AppRuleError(f"Arquivo do app precisa ser um .whl sem caminho: {wheel_name!r}")
    if len(wheel) > MAX_FILE_BYTES:
        raise AppRuleError("Arquivo .whl acima de 4 MiB", 413)
    try:
        with zipfile.ZipFile(io.BytesIO(wheel)) as archive:
            names = [name for name in archive.namelist() if METADATA_PATTERN.fullmatch(name)]
            if len(names) != 1:
                raise AppRuleError("Arquivo .whl sem METADATA")
            if archive.getinfo(names[0]).file_size > MAX_METADATA_BYTES:
                raise AppRuleError("METADATA do .whl acima de 1 MiB")
            raw = archive.read(names[0]).decode("utf-8")
    except (zipfile.BadZipFile, UnicodeDecodeError) as error:
        raise AppRuleError("Arquivo .whl inválido") from error
    metadata = HeaderParser().parsestr(raw)
    for field in REQUIRED_METADATA:
        if not metadata.get(field):
            raise AppRuleError(f"METADATA do .whl sem {field}")
    return metadata


def _require_same_name(name: str, wheel_project: str) -> None:
    if _normalize(wheel_project) != name:
        raise AppRuleError(f"O .whl é do projeto {wheel_project}, não de {name}")


def is_home_file(value: str) -> bool:
    return bool(HOME_FILE_PATTERN.fullmatch(value)) and not {".", ".."} & set(value.split("/"))


def _single_line(text: str) -> bool:
    return "\n" not in text and "\r" not in text


def _normalize(project: str) -> str:
    return re.sub(r"[-_.]+", "-", project).lower()


def _card_texts(app: AppInput) -> list[tuple[str, str]]:
    texts = [("title", app.title or ""), ("description", app.description), ("usage", app.usage)]
    texts += [("requirements", item) for item in app.requirements]
    texts += [(f"settings.{item.name}", f"{item.name} {item.description}") for item in app.settings]
    return texts


def build_app_archive(app: AppInput) -> tuple[bytes, str]:
    files = [
        SkillFileInput(path=APP_ROOT_FILE, content=render_app_md(app)),
        SkillFileInput(path=app.wheel_name, content=base64.b64encode(app.wheel).decode("ascii"), encoding="base64"),
    ]
    try:
        archive, _ = _build_archive(files, required_root_file=APP_ROOT_FILE, label="Aplicativo")
    except ValueError as error:
        raise AppRuleError(str(error), 413) from error
    return archive, hashlib.sha256(app.wheel).hexdigest()


def render_app_md(app: AppInput) -> str:
    lines = [f"# {app.title or app.name}", "", app.description, "", "## Como usar", "", app.usage, ""]
    lines += ["## Comandos", "", f"- Iniciar: `{app.commands.start}`", f"- Verificar: `{app.commands.verify}`", ""]
    if app.requirements:
        lines += ["## Requisitos", "", *[f"- {item}" for item in app.requirements], ""]
    lines += ["## O que precisa configurar", ""]
    if not app.settings:
        lines.append("Nada a configurar.")
    for setting in app.settings:
        lines.append(f"- `{setting.name}`: {setting.description} ({_setting_details(setting)})")
    return "\n".join(lines) + "\n"


def _setting_details(setting: AppSetting) -> str:
    places = []
    if setting.env:
        places.append(f"variável {setting.env}")
    if setting.file and setting.key:
        places.append(f"chave {setting.key} em ~/{setting.file}")
    flags = ["obrigatório" if setting.required else "opcional"]
    if setting.secret:
        flags.append("segredo")
    return ", ".join(flags + [" ou ".join(places)])
