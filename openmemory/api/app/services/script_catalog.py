from __future__ import annotations

import datetime
import logging
from typing import Any

import anyio
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.script_packages import (
    ScriptInput,
    ScriptRuleError,
    content_sha256,
    effect_warning,
    is_same_publication,
    next_version,
    to_registry_resource,
)
from app.utils.agentregistry import AgentRegistryError, AgentRegistryHttpClient

SCRIPT_KIND = "script"
LATEST_TAG = "latest"
VERSION_KEY = "sharemem.io/versao"
AUTHOR_KEY = "sharemem.io/autor"
PUBLISHED_KEY = "sharemem.io/publicado-em"
SHA256_KEY = "sharemem.io/sha256"
OBSOLETE_KEY = "sharemem.io/obsoleto"
OBSOLETE_WARNING = "Script obsoleto"


class PublishResult(BaseModel):
    name: str
    version: str
    sha256: str
    created: bool


class ScriptVersion(BaseModel):
    version: str
    author: str | None
    published_at: str | None
    is_latest: bool


class ScriptCard(BaseModel):
    title: str | None
    description: str
    usage: str
    language: str
    requirements: list[str]
    effects: list[str]
    area: str
    tags: list[str]
    author: str | None
    published_at: str | None


class ScriptView(BaseModel):
    name: str
    version: str | None
    is_latest: bool
    file_name: str
    content: str
    sha256: str
    warning: str | None
    card: ScriptCard
    versions: list[ScriptVersion]


class ScriptCatalog:
    def __init__(
        self,
        client: AgentRegistryHttpClient,
        db: Session,
        auth_headers: dict[str, str] | None,
    ):
        self.client = client
        self.db = db
        self.auth_headers = auth_headers

    async def publish(self, script: ScriptInput, author: str) -> PublishResult:
        try:
            await self._lock_name(script.name)
            items = await self._tag_items(script.name)
            result = await self._apply_new_version(script, author, items)
        except AgentRegistryError as error:
            raise _translate_registry_error(error, script.name, LATEST_TAG) from error
        finally:
            self.db.rollback()
        logging.info(
            f"script_publish name={script.name} version={result.version} "
            f"created={result.created} author={author}"
        )
        return result

    async def get(self, name: str, version: str | None = None) -> ScriptView:
        tag = version or LATEST_TAG
        try:
            resource = await self._get_tag(name, tag)
            items = await self._tag_items(name)
        except AgentRegistryError as error:
            raise _translate_registry_error(error, name, tag) from error
        return _build_view(resource, items)

    async def rollback(self, name: str, version: str, author: str) -> ScriptView:
        view = await self._replace_latest(name, version, author, {VERSION_KEY: version})
        logging.info(f"script_rollback name={name} version={version} author={author}")
        return view

    async def retire(self, name: str, author: str) -> ScriptView:
        view = await self._replace_latest(name, LATEST_TAG, author, {OBSOLETE_KEY: "true"})
        logging.info(f"script_retire name={name} author={author}")
        return view

    async def _replace_latest(
        self, name: str, source_tag: str, author: str, annotations: dict[str, str]
    ) -> ScriptView:
        try:
            await self._lock_name(name)
            source = await self._get_tag(name, source_tag)
            latest = _copy_as_latest(source, author, annotations)
            await self.client.apply_resource(resource=latest, auth_headers=self.auth_headers)
            items = await self._tag_items(name)
        except AgentRegistryError as error:
            raise _translate_registry_error(error, name, source_tag) from error
        finally:
            self.db.rollback()
        return _build_view(latest, items)

    async def _lock_name(self, name: str) -> None:
        if self.db.get_bind().dialect.name != "postgresql":
            return
        await anyio.to_thread.run_sync(
            self.db.execute,
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"script_publish:{name}"},
        )

    async def _get_tag(self, name: str, tag: str) -> dict[str, Any]:
        return await self.client.get_resource(
            kind=SCRIPT_KIND, name=name, tag=tag, auth_headers=self.auth_headers
        )

    async def _tag_items(self, name: str) -> list[dict[str, Any]]:
        payload = await self.client.list_tags(
            kind=SCRIPT_KIND, name=name, auth_headers=self.auth_headers
        )
        return payload.get("items") or []

    async def _apply_new_version(
        self, script: ScriptInput, author: str, items: list[dict[str, Any]]
    ) -> PublishResult:
        sha256 = content_sha256(script.content)
        latest = _find_tag(items, LATEST_TAG) or {}
        latest_annotations = _annotations(latest)
        if latest and is_same_publication(script, latest.get("spec") or {}, latest_annotations.get(SHA256_KEY)):
            current = latest_annotations.get(VERSION_KEY)
            return PublishResult(name=script.name, version=current, sha256=sha256, created=False)
        version = next_version([str((item.get("metadata") or {}).get("tag")) for item in items])
        annotations = {
            VERSION_KEY: version,
            AUTHOR_KEY: author,
            PUBLISHED_KEY: _now(),
            SHA256_KEY: sha256,
        }
        for tag in (version, LATEST_TAG):
            await self.client.apply_resource(
                resource=to_registry_resource(script, tag, annotations),
                auth_headers=self.auth_headers,
            )
        return PublishResult(name=script.name, version=version, sha256=sha256, created=True)


def _translate_registry_error(error: AgentRegistryError, name: str, version: str) -> ScriptRuleError:
    if error.status_code == 404:
        return ScriptRuleError(f"Script não encontrado: {name}@{version}")
    if error.status_code == 429:
        retry_after = getattr(error, "retry_after", None)
        wait = f"em {retry_after} s" if retry_after else "de novo em instantes"
        return ScriptRuleError(f"Limite da Store atingido; tente {wait}")
    if error.status_code == 502:
        return ScriptRuleError("Store indisponível; tente de novo em instantes")
    return ScriptRuleError(error.detail)


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _annotations(resource: dict[str, Any]) -> dict[str, str]:
    return (resource.get("metadata") or {}).get("annotations") or {}


def _find_tag(items: list[dict[str, Any]], tag: str) -> dict[str, Any] | None:
    return next((item for item in items if (item.get("metadata") or {}).get("tag") == tag), None)


def _numbered_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    numbered = [item for item in items if str((item.get("metadata") or {}).get("tag")).isdigit()]
    return sorted(numbered, key=lambda item: int(item["metadata"]["tag"]), reverse=True)


def is_retired(resource: dict[str, Any]) -> bool:
    return _annotations(resource).get(OBSOLETE_KEY) == "true"


def _copy_as_latest(source: dict[str, Any], author: str, extra: dict[str, str]) -> dict[str, Any]:
    annotations = {
        **_annotations(source),
        AUTHOR_KEY: author,
        PUBLISHED_KEY: _now(),
        **extra,
    }
    return {
        "apiVersion": source.get("apiVersion"),
        "kind": source.get("kind"),
        "metadata": {
            "name": source["metadata"]["name"],
            "tag": LATEST_TAG,
            "annotations": annotations,
        },
        "spec": source.get("spec") or {},
    }


def _build_view(resource: dict[str, Any], items: list[dict[str, Any]]) -> ScriptView:
    spec = resource.get("spec") or {}
    latest = _find_tag(items, LATEST_TAG) or {}
    latest_version = _annotations(latest).get(VERSION_KEY)
    version = _annotations(resource).get(VERSION_KEY)
    content = spec.get("content") or ""
    return ScriptView(
        name=resource["metadata"]["name"],
        version=version,
        is_latest=version is not None and version == latest_version,
        file_name=spec.get("fileName") or "",
        content=content,
        sha256=content_sha256(content),
        warning=_view_warning(spec.get("effects") or [], is_retired(latest)),
        card=_build_card(spec, _annotations(resource)),
        versions=_build_versions(items, latest_version),
    )


def _view_warning(effects: list[str], retired: bool) -> str | None:
    warnings = [OBSOLETE_WARNING] if retired else []
    effect = effect_warning(effects)
    if effect:
        warnings.append(effect)
    return ". ".join(warnings) or None


def _build_card(spec: dict[str, Any], annotations: dict[str, str]) -> ScriptCard:
    return ScriptCard(
        title=spec.get("title"),
        description=spec.get("description") or "",
        usage=spec.get("usage") or "",
        language=spec.get("language") or "",
        requirements=spec.get("requirements") or [],
        effects=spec.get("effects") or [],
        area=spec.get("area") or "",
        tags=spec.get("tags") or [],
        author=annotations.get(AUTHOR_KEY),
        published_at=annotations.get(PUBLISHED_KEY),
    )


def _build_versions(items: list[dict[str, Any]], latest_version: str | None) -> list[ScriptVersion]:
    return [
        ScriptVersion(
            version=item["metadata"]["tag"],
            author=_annotations(item).get(AUTHOR_KEY),
            published_at=_annotations(item).get(PUBLISHED_KEY),
            is_latest=item["metadata"]["tag"] == latest_version,
        )
        for item in _numbered_items(items)
    ]
