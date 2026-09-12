"""Validation and deterministic packaging for complete Store Hooks.

A Hook package is the same shape as a Skill package — a validated directory
compressed into a reproducible tar.gz — plus the declarative event map that
the host merges into its settings file. Scripts shipped in the package are
referenced from a command with the ``${HOOK_DIR}`` placeholder, which the
install recipe expands to the directory the package was extracted into.
"""

from __future__ import annotations

import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.skill_packages import (
    MAX_FILES,
    SkillFileInput,
    _build_archive,
    _validate_pt_br,
)

# The placeholder a hook command uses to reach a file shipped in its own
# package. The install recipe replaces it with the extraction directory.
HOOK_DIR_PLACEHOLDER = "${HOOK_DIR}"

# Event names follow the harness convention (PreToolUse, SessionStart, ...).
# Membership is not enforced: harnesses add events over time, and rejecting an
# unknown one would block publishing a hook for a brand-new event.
_EVENT_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,63}$")

MAX_EVENTS = 32
MAX_GROUPS_PER_EVENT = 16
MAX_ENTRIES_PER_GROUP = 16


class HookEntryInput(BaseModel):
    """One hook action, mirroring the registry's HookEntry."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    type: Literal["command", "prompt", "agent", "http", "mcp_tool"]

    command: Optional[str] = Field(default=None, max_length=4000)
    shell: Optional[str] = Field(default=None, max_length=240)

    prompt: Optional[str] = Field(default=None, max_length=8000)
    model: Optional[str] = Field(default=None, max_length=240)

    url: Optional[str] = Field(default=None, max_length=2000)
    headers: Optional[dict[str, str]] = None
    allowed_env_vars: Optional[list[str]] = Field(default=None, alias="allowedEnvVars")

    server: Optional[str] = Field(default=None, max_length=240)
    tool: Optional[str] = Field(default=None, max_length=240)
    input: Optional[dict[str, Any]] = None

    condition: Optional[str] = Field(default=None, alias="if", max_length=2000)
    timeout: Optional[float] = Field(default=None, ge=0)
    status_message: Optional[str] = Field(default=None, alias="statusMessage", max_length=240)
    once: Optional[bool] = None
    is_async: Optional[bool] = Field(default=None, alias="async")
    async_rewake: Optional[bool] = Field(default=None, alias="asyncRewake")

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        if not value.startswith(("http://", "https://")):
            raise ValueError("url deve começar com http:// ou https://")
        return value

    @model_validator(mode="after")
    def validate_required_by_type(self) -> "HookEntryInput":
        """Enforce the field each handler type needs to do anything at all.

        The registry enforces the same rule; catching it here turns a rejected
        publish into a readable message instead of a round trip.
        """
        missing: list[str] = []
        if self.type == "command" and not (self.command or "").strip():
            missing.append("command")
        if self.type in ("prompt", "agent") and not (self.prompt or "").strip():
            missing.append("prompt")
        if self.type == "http" and not (self.url or "").strip():
            missing.append("url")
        if self.type == "mcp_tool":
            if not (self.server or "").strip():
                missing.append("server")
            if not (self.tool or "").strip():
                missing.append("tool")
        if missing:
            raise ValueError(
                f"type={self.type} exige: {', '.join(missing)}"
            )
        return self

    def to_registry_entry(self) -> dict[str, Any]:
        """Serialize to the registry's HookEntry JSON shape."""
        return self.model_dump(by_alias=True, exclude_none=True)


class HookMatcherGroupInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    matcher: Optional[str] = Field(default=None, max_length=240)
    hooks: list[HookEntryInput] = Field(min_length=1, max_length=MAX_ENTRIES_PER_GROUP)

    def to_registry_group(self) -> dict[str, Any]:
        group: dict[str, Any] = {"hooks": [entry.to_registry_entry() for entry in self.hooks]}
        if self.matcher:
            group["matcher"] = self.matcher
        return group


class HookPackageInput(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    tag: str = Field(default="latest", min_length=1, max_length=128)
    title: str | None = Field(default=None, max_length=240)
    description: str = Field(min_length=1, max_length=4000)
    language: str = "pt-BR"
    events: dict[str, list[HookMatcherGroupInput]] = Field(min_length=1)
    files: list[SkillFileInput] = Field(min_length=1, max_length=MAX_FILES)

    @field_validator("language")
    @classmethod
    def validate_language(cls, value: str) -> str:
        if value != "pt-BR":
            raise ValueError("Hooks da Store devem declarar language=pt-BR")
        return value

    @field_validator("events")
    @classmethod
    def validate_events(
        cls, value: dict[str, list[HookMatcherGroupInput]]
    ) -> dict[str, list[HookMatcherGroupInput]]:
        if len(value) > MAX_EVENTS:
            raise ValueError(f"no máximo {MAX_EVENTS} eventos por Hook")
        for event, groups in value.items():
            if not _EVENT_NAME.match(event):
                raise ValueError(f"nome de evento inválido: {event}")
            if not groups:
                raise ValueError(f"evento sem grupos de matcher: {event}")
            if len(groups) > MAX_GROUPS_PER_EVENT:
                raise ValueError(f"no máximo {MAX_GROUPS_PER_EVENT} grupos em {event}")
        return value

    def to_registry_events(self) -> dict[str, list[dict[str, Any]]]:
        return {
            event: [group.to_registry_group() for group in groups]
            for event, groups in self.events.items()
        }


def build_hook_archive(payload: HookPackageInput) -> tuple[bytes, list[dict[str, Any]]]:
    """Validate and pack a Hook directory, requiring HOOK.md at the root."""
    _validate_pt_br(payload.description, "description")
    return _build_archive(payload.files, required_root_file="HOOK.md", label="Hook")


def referenced_package_paths(payload: HookPackageInput) -> set[str]:
    """Return the in-package paths every ${HOOK_DIR} command points at."""
    referenced: set[str] = set()
    for groups in payload.events.values():
        for group in groups:
            for entry in group.hooks:
                if not entry.command:
                    continue
                for match in re.finditer(
                    re.escape(HOOK_DIR_PLACEHOLDER) + r"/([\w./-]+)", entry.command
                ):
                    referenced.add(match.group(1))
    return referenced


def validate_command_references(payload: HookPackageInput) -> None:
    """Fail publication when a command points at a file the package lacks.

    A hook whose command references a missing script installs cleanly and then
    fails on every event, so the check belongs at publish time.
    """
    shipped = {file.path for file in payload.files}
    missing = sorted(path for path in referenced_package_paths(payload) if path not in shipped)
    if missing:
        raise ValueError("comando referencia arquivo ausente no pacote: " + ", ".join(missing))
