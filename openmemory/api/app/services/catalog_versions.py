from __future__ import annotations

import datetime
from typing import Any

import anyio
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.script_packages import next_version
from app.utils.agentregistry import AgentRegistryError, AgentRegistryHttpClient

LATEST_TAG = "latest"
VERSION_KEY = "sharemem.io/versao"
AUTHOR_KEY = "sharemem.io/autor"
PUBLISHED_KEY = "sharemem.io/publicado-em"
SHA256_KEY = "sharemem.io/sha256"


class PublishResult(BaseModel):
    name: str
    version: str
    sha256: str
    created: bool


class CatalogVersion(BaseModel):
    version: str
    author: str | None
    published_at: str | None
    is_latest: bool


class VersionedCatalog:
    kind: str
    label: str
    rule_error: type[Exception]

    def __init__(
        self,
        client: AgentRegistryHttpClient,
        db: Session,
        auth_headers: dict[str, str] | None,
    ):
        self.client = client
        self.db = db
        self.auth_headers = auth_headers

    async def _lock_name(self, name: str) -> None:
        if self.db.get_bind().dialect.name != "postgresql":
            return
        await anyio.to_thread.run_sync(
            self.db.execute,
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"{self.kind}_publish:{name}"},
        )

    async def _get_tag(self, name: str, tag: str) -> dict[str, Any]:
        return await self.client.get_resource(
            kind=self.kind, name=name, tag=tag, auth_headers=self.auth_headers
        )

    async def _tag_items(self, name: str) -> list[dict[str, Any]]:
        payload = await self.client.list_tags(
            kind=self.kind, name=name, auth_headers=self.auth_headers
        )
        return payload.get("items") or []

    def _translate_registry_error(self, error: AgentRegistryError, name: str, version: str) -> Exception:
        if error.status_code == 404:
            return self.rule_error(f"{self.label} não encontrado: {name}@{version}", 404)
        if error.status_code == 429:
            retry_after = getattr(error, "retry_after", None)
            wait = f"em {retry_after} s" if retry_after else "de novo em instantes"
            return self.rule_error(f"Limite da Store atingido; tente {wait}", 429)
        if error.status_code == 502:
            return self.rule_error("Store indisponível; tente de novo em instantes", 502)
        return self.rule_error(error.detail, error.status_code)


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def annotations_of(resource: dict[str, Any]) -> dict[str, str]:
    return (resource.get("metadata") or {}).get("annotations") or {}


def find_tag(items: list[dict[str, Any]], tag: str) -> dict[str, Any] | None:
    return next((item for item in items if (item.get("metadata") or {}).get("tag") == tag), None)


def next_tag(items: list[dict[str, Any]]) -> str:
    return next_version([str((item.get("metadata") or {}).get("tag")) for item in items])


def copy_as_latest(source: dict[str, Any], author: str, extra: dict[str, str]) -> dict[str, Any]:
    annotations = {
        **annotations_of(source),
        AUTHOR_KEY: author,
        PUBLISHED_KEY: now(),
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


def build_versions(items: list[dict[str, Any]], latest_version: str | None) -> list[CatalogVersion]:
    numbered = [item for item in items if str((item.get("metadata") or {}).get("tag")).isdigit()]
    numbered.sort(key=lambda item: int(item["metadata"]["tag"]), reverse=True)
    return [
        CatalogVersion(
            version=item["metadata"]["tag"],
            author=annotations_of(item).get(AUTHOR_KEY),
            published_at=annotations_of(item).get(PUBLISHED_KEY),
            is_latest=item["metadata"]["tag"] == latest_version,
        )
        for item in numbered
    ]
