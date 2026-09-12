"""Validation and deterministic packaging for complete Store Skills."""

from __future__ import annotations

import base64
import binascii
import gzip
import io
import re
import tarfile
from typing import Any

from pydantic import BaseModel, Field, field_validator

MAX_ARCHIVE_BYTES = 16 << 20
MAX_UNCOMPRESSED_BYTES = 32 << 20
MAX_FILE_BYTES = 4 << 20
MAX_FILES = 256
_SAFE_PATH = re.compile(r"^[^\\\x00]+$")
_PT_MARKERS = {
    " a ", " ao ", " aos ", " as ", " com ", " da ", " das ", " de ",
    " do ", " dos ", " e ", " em ", " para ", " por ", " que ", " uma ",
    " não ", " seu ", " sua ", " usuário ", " instruções ",
}


class SkillFileInput(BaseModel):
    path: str = Field(min_length=1, max_length=240)
    content: str = Field(min_length=0)
    encoding: str = "utf-8"
    mode: int = Field(default=0o644, ge=0o600, le=0o755)

    @field_validator("encoding")
    @classmethod
    def validate_encoding(cls, value: str) -> str:
        if value not in {"utf-8", "base64"}:
            raise ValueError("encoding deve ser utf-8 ou base64")
        return value


class SkillPackageInput(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    tag: str = Field(default="latest", min_length=1, max_length=128)
    title: str | None = Field(default=None, max_length=240)
    description: str = Field(min_length=1, max_length=4000)
    language: str = "pt-BR"
    files: list[SkillFileInput] = Field(min_length=1, max_length=MAX_FILES)

    @field_validator("language")
    @classmethod
    def validate_language(cls, value: str) -> str:
        if value != "pt-BR":
            raise ValueError("Skills da Store devem declarar language=pt-BR")
        return value


def _decode_file(file: SkillFileInput) -> bytes:
    if file.encoding == "utf-8":
        return file.content.encode("utf-8")
    try:
        return base64.b64decode(file.content, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError(f"conteúdo base64 inválido para {file.path}") from exc


def _validate_path(path: str) -> None:
    if not _SAFE_PATH.match(path) or path.startswith("/") or path.endswith("/"):
        raise ValueError(f"caminho inválido: {path}")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"caminho inseguro: {path}")


def _validate_pt_br(text: str, field: str) -> None:
    normalized = f" {text.lower()} "
    if len(normalized.strip()) < 20:
        return
    if not any(marker in normalized for marker in _PT_MARKERS) and not any(
        char in normalized for char in "ãõáéíóúçâêô"
    ):
        raise ValueError(f"{field} deve estar em PT-BR")


def build_skill_archive(payload: SkillPackageInput) -> tuple[bytes, list[dict[str, Any]]]:
    _validate_pt_br(payload.description, "description")
    return _build_archive(payload.files, required_root_file="SKILL.md", label="Skill")


def _build_archive(
    inputs: list[SkillFileInput],
    *,
    required_root_file: str,
    label: str,
) -> tuple[bytes, list[dict[str, Any]]]:
    """Pack a validated directory into a reproducible tar.gz.

    Shared by every packaged kind (Skill, Hook): the limits, the PT-BR check on
    text files and the deterministic archive layout are identical; only the
    mandatory root file differs.
    """
    files: dict[str, tuple[bytes, int]] = {}
    total = 0
    for file in inputs:
        _validate_path(file.path)
        if file.path in files:
            raise ValueError(f"arquivo duplicado: {file.path}")
        content = _decode_file(file)
        if len(content) > MAX_FILE_BYTES:
            raise ValueError(f"arquivo excede {MAX_FILE_BYTES} bytes: {file.path}")
        total += len(content)
        if total > MAX_UNCOMPRESSED_BYTES:
            raise ValueError("conteúdo descompactado excede o limite da Store")
        if file.path.endswith((".md", ".txt")):
            try:
                _validate_pt_br(content.decode("utf-8"), file.path)
            except UnicodeDecodeError as exc:
                raise ValueError(f"arquivo textual inválido: {file.path}") from exc
        files[file.path] = (content, file.mode)

    if required_root_file not in files:
        raise ValueError(f"{required_root_file} é obrigatório na raiz do {label}")

    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as archive:
            for path in sorted(files):
                content, mode = files[path]
                info = tarfile.TarInfo(path)
                info.size = len(content)
                info.mode = mode
                info.mtime = 0
                archive.addfile(info, io.BytesIO(content))
    data = output.getvalue()
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ValueError("pacote compactado excede o limite da Store")
    inventory = [
        {"path": path, "size": len(content), "mode": mode}
        for path, (content, mode) in sorted(files.items())
    ]
    return data, inventory
