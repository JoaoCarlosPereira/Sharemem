from __future__ import annotations

import datetime
import logging
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.models import ScriptDownload, get_current_utc_time
from app.services.catalog_versions import (
    AUTHOR_KEY,
    LATEST_TAG,
    PUBLISHED_KEY,
    SHA256_KEY,
    VERSION_KEY,
    PublishResult,
    VersionedCatalog,
    annotations_of,
    build_versions,
    copy_as_latest,
    find_tag,
    next_tag,
    now,
)
from app.services.catalog_versions import CatalogVersion as ScriptVersion
from app.services.script_packages import (
    ScriptInput,
    ScriptRuleError,
    content_sha256,
    effect_warning,
    is_same_publication,
    to_registry_resource,
)
from app.utils.agentregistry import AgentRegistryError

OBSOLETE_KEY = "sharemem.io/obsoleto"
OBSOLETE_WARNING = "Script obsoleto"
DOWNLOAD_WINDOW = datetime.timedelta(days=30)


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
    downloads_30d: int | None = None


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


class ScriptCatalog(VersionedCatalog):
    kind = "script"
    label = "Script"
    rule_error = ScriptRuleError

    async def publish(self, script: ScriptInput, author: str) -> PublishResult:
        try:
            await self._lock_name(script.name)
            items = await self._tag_items(script.name)
            result = await self._apply_new_version(script, author, items)
        except AgentRegistryError as error:
            raise self._translate_registry_error(error, script.name, LATEST_TAG) from error
        finally:
            self.db.rollback()
        logging.info(
            f"script_publish name={script.name} version={result.version} "
            f"created={result.created} author={author}"
        )
        return result

    async def get(
        self, name: str, version: str | None = None, hostname: str | None = None
    ) -> ScriptView:
        tag = version or LATEST_TAG
        try:
            resource = await self._get_tag(name, tag)
            items = await self._tag_items(name)
        except AgentRegistryError as error:
            raise self._translate_registry_error(error, name, tag) from error
        view = _build_view(resource, items)
        if hostname:
            self._record_download(name, view.version or tag, hostname)
        view.card.downloads_30d = self._count_downloads(name)
        return view

    def _record_download(self, name: str, version: str, hostname: str) -> None:
        try:
            self.db.add(ScriptDownload(name=name, version=version, hostname=hostname))
            self.db.commit()
        except SQLAlchemyError:
            self.db.rollback()
            logging.exception(f"script_download name={name} version={version} hostname={hostname}")

    def _count_downloads(self, name: str) -> int | None:
        since = get_current_utc_time() - DOWNLOAD_WINDOW
        try:
            return self.db.scalar(
                select(func.count())
                .select_from(ScriptDownload)
                .where(ScriptDownload.name == name, ScriptDownload.downloaded_at >= since)
            )
        except SQLAlchemyError:
            self.db.rollback()
            logging.exception(f"script_download_count name={name}")
            return None

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
            latest = copy_as_latest(source, author, annotations)
            await self.client.apply_resource(resource=latest, auth_headers=self.auth_headers)
            items = await self._tag_items(name)
        except AgentRegistryError as error:
            raise self._translate_registry_error(error, name, source_tag) from error
        finally:
            self.db.rollback()
        return _build_view(latest, items)

    async def _apply_new_version(
        self, script: ScriptInput, author: str, items: list[dict[str, Any]]
    ) -> PublishResult:
        sha256 = content_sha256(script.content)
        latest = find_tag(items, LATEST_TAG) or {}
        latest_annotations = annotations_of(latest)
        if latest and is_same_publication(script, latest.get("spec") or {}, latest_annotations.get(SHA256_KEY)):
            current = latest_annotations.get(VERSION_KEY)
            return PublishResult(name=script.name, version=current, sha256=sha256, created=False)
        version = next_tag(items)
        annotations = {
            VERSION_KEY: version,
            AUTHOR_KEY: author,
            PUBLISHED_KEY: now(),
            SHA256_KEY: sha256,
        }
        for tag in (version, LATEST_TAG):
            await self.client.apply_resource(
                resource=to_registry_resource(script, tag, annotations),
                auth_headers=self.auth_headers,
            )
        return PublishResult(name=script.name, version=version, sha256=sha256, created=True)


def is_retired(resource: dict[str, Any]) -> bool:
    return annotations_of(resource).get(OBSOLETE_KEY) == "true"


def _build_view(resource: dict[str, Any], items: list[dict[str, Any]]) -> ScriptView:
    spec = resource.get("spec") or {}
    latest = find_tag(items, LATEST_TAG) or {}
    latest_version = annotations_of(latest).get(VERSION_KEY)
    version = annotations_of(resource).get(VERSION_KEY)
    content = spec.get("content") or ""
    return ScriptView(
        name=resource["metadata"]["name"],
        version=version,
        is_latest=version is not None and version == latest_version,
        file_name=spec.get("fileName") or "",
        content=content,
        sha256=content_sha256(content),
        warning=_view_warning(spec.get("effects") or [], is_retired(latest)),
        card=_build_card(spec, annotations_of(resource)),
        versions=build_versions(items, latest_version),
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
