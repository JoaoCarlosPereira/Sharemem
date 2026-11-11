from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel

from app.services.app_packages import AppCommands, AppInput, AppRuleError, AppSetting, build_app_archive
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
from app.services.catalog_versions import CatalogVersion as AppVersion
from app.utils.agentregistry import AgentRegistryError


class AppCard(BaseModel):
    title: str | None
    description: str
    usage: str
    requirements: list[str]
    effects: list[str]
    area: str
    tags: list[str]
    author: str | None
    published_at: str | None


class AppView(BaseModel):
    name: str
    version: str | None
    is_latest: bool
    sha256: str
    wheel_name: str
    python_requires: str
    card: AppCard
    commands: AppCommands
    settings: list[AppSetting]
    versions: list[AppVersion]


class AppCatalog(VersionedCatalog):
    kind = "app"
    label = "Aplicativo"
    rule_error = AppRuleError

    async def publish(self, app: AppInput, author: str) -> PublishResult:
        archive, sha256 = build_app_archive(app)
        try:
            await self._lock_name(app.name)
            items = await self._tag_items(app.name)
            result = await self._apply_new_version(app, archive, sha256, author, items)
        except AgentRegistryError as error:
            raise self._translate_registry_error(error, app.name, LATEST_TAG) from error
        finally:
            self.db.rollback()
        logging.info(
            f"app_publish name={app.name} version={result.version} "
            f"created={result.created} author={author} size={len(archive)}"
        )
        return result

    async def get(self, name: str, version: str | None = None) -> AppView:
        tag = version or LATEST_TAG
        try:
            resource = await self._get_tag(name, tag)
            items = await self._tag_items(name)
        except AgentRegistryError as error:
            raise self._translate_registry_error(error, name, tag) from error
        return _build_view(resource, items)

    async def read_artifact(self, name: str, version: str) -> bytes:
        try:
            archive, _ = await self.client.get_package_artifact(
                kind=self.kind, name=name, tag=version, auth_headers=self.auth_headers
            )
        except AgentRegistryError as error:
            raise self._translate_registry_error(error, name, version) from error
        return archive

    async def rollback(self, name: str, version: str, author: str) -> AppView:
        try:
            await self._lock_name(name)
            source = await self._get_tag(name, version)
            archive, _ = await self.client.get_package_artifact(
                kind=self.kind, name=name, tag=version, auth_headers=self.auth_headers
            )
            latest = copy_as_latest(source, author, {})
            await self._apply_with_artifact(latest, archive)
            items = await self._tag_items(name)
        except AgentRegistryError as error:
            raise self._translate_registry_error(error, name, version) from error
        finally:
            self.db.rollback()
        logging.info(f"app_rollback name={name} version={version} author={author}")
        return _build_view(latest, items)

    async def _apply_new_version(
        self, app: AppInput, archive: bytes, sha256: str, author: str, items: list[dict[str, Any]]
    ) -> PublishResult:
        latest = find_tag(items, LATEST_TAG) or {}
        latest_annotations = annotations_of(latest)
        if latest_annotations.get(SHA256_KEY) == sha256:
            current = latest_annotations.get(VERSION_KEY)
            return PublishResult(name=app.name, version=current, sha256=sha256, created=False)
        version = next_tag(items)
        annotations = {
            VERSION_KEY: version,
            AUTHOR_KEY: author,
            PUBLISHED_KEY: now(),
            SHA256_KEY: sha256,
        }
        for tag in (version, LATEST_TAG):
            await self._apply_with_artifact(_to_registry_resource(app, tag, annotations), archive)
        return PublishResult(name=app.name, version=version, sha256=sha256, created=True)

    async def _apply_with_artifact(self, resource: dict[str, Any], archive: bytes) -> None:
        metadata = resource["metadata"]
        await self.client.apply_resource(resource=resource, auth_headers=self.auth_headers)
        await self.client.put_package_artifact(
            kind=self.kind,
            name=metadata["name"],
            tag=metadata["tag"],
            archive=archive,
            auth_headers=self.auth_headers,
        )


def _to_registry_resource(app: AppInput, tag: str, annotations: dict[str, str]) -> dict[str, Any]:
    optional = {"title": app.title, "requirements": app.requirements, "tags": app.tags, "settings": _settings_of(app)}
    spec = {
        "description": app.description,
        "usage": app.usage,
        "effects": list(app.effects),
        "area": app.area,
        "wheel": app.wheel_name,
        "pythonRequires": app.python_requires,
        "commands": app.commands.model_dump(),
    }
    return {
        "apiVersion": "ar.dev/v1alpha1",
        "kind": "LocalApp",
        "metadata": {"name": app.name, "tag": tag, "annotations": annotations},
        "spec": {**{key: value for key, value in optional.items() if value}, **spec},
    }


def _settings_of(app: AppInput) -> list[dict[str, Any]]:
    return [setting.model_dump(exclude_none=True) for setting in app.settings]


def _build_view(resource: dict[str, Any], items: list[dict[str, Any]]) -> AppView:
    spec = resource.get("spec") or {}
    annotations = annotations_of(resource)
    latest_version = annotations_of(find_tag(items, LATEST_TAG) or {}).get(VERSION_KEY)
    version = annotations.get(VERSION_KEY)
    return AppView(
        name=resource["metadata"]["name"],
        version=version,
        is_latest=version is not None and version == latest_version,
        sha256=annotations.get(SHA256_KEY) or "",
        wheel_name=spec.get("wheel") or "",
        python_requires=spec.get("pythonRequires") or "",
        card=AppCard(
            title=spec.get("title"),
            description=spec.get("description") or "",
            usage=spec.get("usage") or "",
            requirements=spec.get("requirements") or [],
            effects=spec.get("effects") or [],
            area=spec.get("area") or "",
            tags=spec.get("tags") or [],
            author=annotations.get(AUTHOR_KEY),
            published_at=annotations.get(PUBLISHED_KEY),
        ),
        commands=AppCommands(**(spec.get("commands") or {})),
        settings=[AppSetting(**{"required": False, "secret": False, **item}) for item in spec.get("settings") or []],
        versions=build_versions(items, latest_version),
    )
