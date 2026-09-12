"""HTTP client for the internal AgentRegistry catalog.

The OpenMemory API uses this as the single integration seam for the store UI,
install recipes and MCP catalog tools. It never talks to Qdrant and only calls
the AgentRegistry REST API.
"""

from __future__ import annotations

import json
import base64
import hashlib
import os
from typing import Any, Optional
from urllib.parse import quote

import httpx

DEFAULT_REGISTRY_BASE_URL = "http://agentregistry:8080"
DEFAULT_REGISTRY_TIMEOUT_SECONDS = 5.0
DEFAULT_SEARCH_LIMIT = 20
MAX_SEARCH_LIMIT = 100

KIND_TO_REGISTRY_COLLECTION: dict[str, str] = {
    "agent": "agents",
    "hook": "hooks",
    "mcpserver": "mcpservers",
    "plugin": "plugins",
    "prompt": "prompts",
    "skill": "skills",
}

REGISTRY_KIND_TO_CATALOG_KIND: dict[str, str] = {
    "Agent": "agent",
    "Hook": "hook",
    "MCPServer": "mcpserver",
    "Plugin": "plugin",
    "Prompt": "prompt",
    "Skill": "skill",
}

# Kinds published as a complete directory: the metadata row carries the
# declarative body and an immutable tar.gz artifact carries the files.
PACKAGE_MEDIA_TYPES: dict[str, str] = {
    "skill": "application/vnd.agentregistry.skill.v1.tar+gzip",
    "hook": "application/vnd.agentregistry.hook.v1.tar+gzip",
}


class AgentRegistryError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class AgentRegistryValidationError(AgentRegistryError):
    def __init__(self, detail: str):
        super().__init__(400, detail)


class AgentRegistryResourceNotFound(AgentRegistryError):
    def __init__(self, detail: str = "recurso não encontrado no AgentRegistry"):
        super().__init__(404, detail)


class AgentRegistryHttpClient:
    """Small async client for the internal AgentRegistry service."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout_seconds: float = DEFAULT_REGISTRY_TIMEOUT_SECONDS,
    ):
        raw_base_url = (
            base_url
            or os.getenv("AGENT_REGISTRY_BASE_URL")
            or os.getenv("AGENT_REGISTRY_URL")
            or os.getenv("AGENT_REGISTRY_INTERNAL_URL")
            or DEFAULT_REGISTRY_BASE_URL
        )
        self.base_url = raw_base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    async def list_resources(
        self,
        *,
        kind: str,
        namespace: str = "default",
        limit: int = DEFAULT_SEARCH_LIMIT,
        cursor: Optional[str] = None,
        auth_headers: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        safe_kind = validate_catalog_kind(kind)
        safe_limit = clamp_limit(limit)
        collection = KIND_TO_REGISTRY_COLLECTION[safe_kind]
        params: dict[str, Any] = {"namespace": namespace or "default", "limit": safe_limit}
        if cursor:
            params["cursor"] = cursor
        response = await self._request(
            "GET",
            f"/v0/{collection}",
            params=params,
            auth_headers=auth_headers,
        )
        return response

    async def get_resource(
        self,
        *,
        kind: str,
        name: str,
        tag: Optional[str] = None,
        namespace: str = "default",
        auth_headers: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        safe_kind = validate_catalog_kind(kind)
        safe_name = validate_path_segment(name, "name")
        safe_tag = validate_path_segment(tag, "tag") if tag else None
        collection = KIND_TO_REGISTRY_COLLECTION[safe_kind]
        path = f"/v0/{collection}/{quote(safe_name, safe='')}"
        if safe_tag:
            path = f"{path}/{quote(safe_tag, safe='')}"
        return await self._request(
            "GET",
            path,
            params={"namespace": namespace or "default"},
            auth_headers=auth_headers,
        )

    async def apply_resource(
        self,
        *,
        resource: dict[str, Any],
        dry_run: bool = False,
        auth_headers: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        if not isinstance(resource, dict) or not resource:
            raise AgentRegistryValidationError("resource deve ser um objeto v1alpha1")
        body = json.dumps(resource, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/yaml"}
        return await self._request(
            "POST",
            "/v0/apply",
            params={"dryRun": str(bool(dry_run)).lower()},
            content=body,
            headers=headers,
            auth_headers=auth_headers,
        )

    async def put_package_artifact(
        self,
        *,
        kind: str,
        name: str,
        tag: str,
        archive: bytes,
        namespace: str = "default",
        auth_headers: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """Upload the immutable package of a packaged kind (skill, hook)."""
        safe_kind = validate_package_kind(kind)
        safe_name = validate_path_segment(name, "name")
        safe_tag = validate_path_segment(tag, "tag")
        collection = KIND_TO_REGISTRY_COLLECTION[safe_kind]
        digest = hashlib.sha256(archive).digest()
        response = await self._request_raw(
            "PUT",
            f"/v0/{collection}/{quote(safe_name, safe='')}/{quote(safe_tag, safe='')}/artifact",
            params={"namespace": namespace or "default"},
            content=archive,
            headers={
                "Content-Type": PACKAGE_MEDIA_TYPES[safe_kind],
                "Digest": "sha-256=" + base64.b64encode(digest).decode("ascii"),
            },
            auth_headers=auth_headers,
        )
        return response

    async def put_skill_artifact(
        self,
        *,
        name: str,
        tag: str,
        archive: bytes,
        namespace: str = "default",
        auth_headers: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        return await self.put_package_artifact(
            kind="skill",
            name=name,
            tag=tag,
            archive=archive,
            namespace=namespace,
            auth_headers=auth_headers,
        )

    async def get_package_download(
        self,
        *,
        kind: str,
        name: str,
        tag: str,
        namespace: str = "default",
        auth_headers: Optional[dict[str, str]] = None,
    ) -> tuple[bytes, dict[str, str]]:
        """Download a packaged kind's directory re-zipped by the registry."""
        safe_kind = validate_package_kind(kind)
        safe_name = validate_path_segment(name, "name")
        safe_tag = validate_path_segment(tag, "tag")
        collection = KIND_TO_REGISTRY_COLLECTION[safe_kind]
        return await self._request_bytes(
            "GET",
            f"/v0/{collection}/{quote(safe_name, safe='')}/{quote(safe_tag, safe='')}/download",
            params={"namespace": namespace or "default"},
            auth_headers=auth_headers,
        )

    async def get_skill_download(
        self,
        *,
        name: str,
        tag: str,
        namespace: str = "default",
        auth_headers: Optional[dict[str, str]] = None,
    ) -> tuple[bytes, dict[str, str]]:
        return await self.get_package_download(
            kind="skill", name=name, tag=tag, namespace=namespace, auth_headers=auth_headers
        )

    async def get_skill_artifact(
        self,
        *,
        name: str,
        tag: str,
        namespace: str = "default",
        auth_headers: Optional[dict[str, str]] = None,
    ) -> tuple[bytes, dict[str, str]]:
        """Fetch the immutable tar.gz artifact exactly as stored.

        Unlike ``get_skill_download``, which re-zips the extracted files and
        therefore changes the bytes, this returns the payload whose sha256 is
        the digest published in ``status.resolvedSource.artifact``. Install
        recipes verify against that digest, so the bytes must not be rebuilt.
        """
        return await self.get_package_artifact(
            kind="skill", name=name, tag=tag, namespace=namespace, auth_headers=auth_headers
        )

    async def get_package_artifact(
        self,
        *,
        kind: str,
        name: str,
        tag: str,
        namespace: str = "default",
        auth_headers: Optional[dict[str, str]] = None,
    ) -> tuple[bytes, dict[str, str]]:
        """Fetch a packaged kind's artifact byte-for-byte (digest-stable)."""
        safe_kind = validate_package_kind(kind)
        safe_name = validate_path_segment(name, "name")
        safe_tag = validate_path_segment(tag, "tag")
        collection = KIND_TO_REGISTRY_COLLECTION[safe_kind]
        return await self._request_bytes(
            "GET",
            f"/v0/{collection}/{quote(safe_name, safe='')}/{quote(safe_tag, safe='')}/artifact",
            params={"namespace": namespace or "default"},
            auth_headers=auth_headers,
        )

    async def delete_resource(
        self,
        *,
        kind: str,
        name: str,
        tag: str,
        namespace: str = "default",
        auth_headers: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        safe_kind = validate_catalog_kind(kind)
        safe_name = validate_path_segment(name, "name")
        safe_tag = validate_path_segment(tag, "tag")
        response = await self._request_response(
            "DELETE",
            f"/v0/{KIND_TO_REGISTRY_COLLECTION[safe_kind]}/{quote(safe_name, safe='')}/{quote(safe_tag, safe='')}",
            params={"namespace": namespace or "default"},
            auth_headers=auth_headers,
        )
        return {"status_code": response.status_code}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        content: Optional[bytes] = None,
        headers: Optional[dict[str, str]] = None,
        auth_headers: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        request_headers = self._headers(headers=headers, auth_headers=auth_headers)
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            try:
                response = await client.request(
                    method,
                    f"{self.base_url}{path}",
                    params=params,
                    content=content,
                    headers=request_headers,
                )
            except httpx.RequestError as exc:
                raise AgentRegistryError(502, "AgentRegistry indisponível") from exc
        return _json_response(response)

    async def _request_raw(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        content: Optional[bytes] = None,
        headers: Optional[dict[str, str]] = None,
        auth_headers: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        response = await self._request_response(method, path, params=params, content=content, headers=headers, auth_headers=auth_headers)
        return {"status_code": response.status_code, "headers": dict(response.headers)}

    async def _request_bytes(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        auth_headers: Optional[dict[str, str]] = None,
    ) -> tuple[bytes, dict[str, str]]:
        response = await self._request_response(method, path, params=params, auth_headers=auth_headers)
        return response.content, dict(response.headers)

    async def _request_response(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        content: Optional[bytes] = None,
        headers: Optional[dict[str, str]] = None,
        auth_headers: Optional[dict[str, str]] = None,
    ) -> httpx.Response:
        request_headers = self._headers(headers=headers, auth_headers=auth_headers)
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            try:
                response = await client.request(
                    method,
                    f"{self.base_url}{path}",
                    params=params,
                    content=content,
                    headers=request_headers,
                )
            except httpx.RequestError as exc:
                raise AgentRegistryError(502, "AgentRegistry indisponível") from exc
        if response.status_code == 404:
            raise AgentRegistryResourceNotFound()
        if response.status_code in (401, 403):
            raise AgentRegistryError(response.status_code, "AgentRegistry negou acesso ao recurso")
        if response.status_code >= 400:
            raise AgentRegistryError(response.status_code, f"AgentRegistry retornou HTTP {response.status_code}")
        return response

    def _headers(
        self,
        *,
        headers: Optional[dict[str, str]] = None,
        auth_headers: Optional[dict[str, str]] = None,
    ) -> dict[str, str]:
        merged = {"Accept": "application/json"}
        if headers:
            merged.update(headers)
        if auth_headers:
            merged.update(auth_headers)
        elif token := _env_auth_token():
            merged["Authorization"] = f"Bearer {token}"
        return merged


def validate_catalog_kind(kind: str) -> str:
    candidate = (kind or "").strip().lower()
    if candidate not in KIND_TO_REGISTRY_COLLECTION:
        raise AgentRegistryValidationError("kind inválido")
    return candidate


def validate_package_kind(kind: str) -> str:
    """Validate a kind that ships a complete directory (skill, hook)."""
    candidate = (kind or "").strip().lower()
    if candidate not in PACKAGE_MEDIA_TYPES:
        raise AgentRegistryValidationError("kind não possui pacote de arquivos")
    return candidate


def validate_path_segment(value: Optional[str], field_name: str) -> str:
    candidate = (value or "").strip()
    if not candidate:
        raise AgentRegistryValidationError(f"{field_name} obrigatório")
    if "\x00" in candidate or "\\" in candidate:
        raise AgentRegistryValidationError(f"{field_name} contém caracteres inseguros")
    if any(part in ("", ".", "..") for part in candidate.split("/")):
        raise AgentRegistryValidationError(f"{field_name} contém path traversal")
    return candidate


def clamp_limit(limit: int) -> int:
    try:
        parsed = int(limit)
    except (TypeError, ValueError):
        parsed = DEFAULT_SEARCH_LIMIT
    return max(1, min(parsed, MAX_SEARCH_LIMIT))


def summarize_resource(resource: dict[str, Any]) -> dict[str, Any]:
    metadata = resource.get("metadata") if isinstance(resource.get("metadata"), dict) else {}
    spec = resource.get("spec") if isinstance(resource.get("spec"), dict) else {}
    kind = REGISTRY_KIND_TO_CATALOG_KIND.get(str(resource.get("kind") or ""), str(resource.get("kind") or "").lower())
    return {
        "kind": kind,
        "registry_kind": resource.get("kind"),
        "namespace": metadata.get("namespace") or "default",
        "name": metadata.get("name"),
        "tag": metadata.get("tag"),
        "title": spec.get("title") or metadata.get("name"),
        "description": spec.get("description"),
        "labels": metadata.get("labels") or {},
        "annotations": metadata.get("annotations") or {},
    }


def resource_matches_query(resource: dict[str, Any], query: str) -> bool:
    needle = (query or "").strip().lower()
    if not needle:
        return True
    summary = summarize_resource(resource)
    haystack = " ".join(
        str(value)
        for value in (
            summary.get("kind"),
            summary.get("registry_kind"),
            summary.get("name"),
            summary.get("tag"),
            summary.get("title"),
            summary.get("description"),
            summary.get("labels"),
            summary.get("annotations"),
        )
        if value
    ).lower()
    return needle in haystack


def _json_response(response: httpx.Response) -> dict[str, Any]:
    if response.status_code == 404:
        raise AgentRegistryResourceNotFound()
    if response.status_code in (401, 403):
        raise AgentRegistryError(response.status_code, "AgentRegistry negou acesso ao recurso")
    if response.status_code >= 400:
        raise AgentRegistryError(502, f"AgentRegistry retornou HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError as exc:
        raise AgentRegistryError(502, "AgentRegistry retornou JSON inválido") from exc
    if not isinstance(data, dict):
        raise AgentRegistryError(502, "AgentRegistry retornou payload inválido")
    return data


def _env_auth_token() -> Optional[str]:
    for name in ("AGENT_REGISTRY_AUTH_TOKEN", "AGENT_REGISTRY_BEARER_TOKEN"):
        token = (os.getenv(name) or "").strip()
        if token:
            return token
    return None


def legacy_registry_auth_allowed() -> bool:
    """Whether AgentRegistry should accept the same LAN legacy shim as OpenMemory.

    AgentRegistry honors ``Bearer local`` only when ``MEM0_AUTH_ALLOW_LEGACY=1``
    (compose default). OpenMemory ``AUTH_MODE=warn|off`` also lets hostname-only
    MCP through — synthesize the shim so catalog tools match memory tools.
    """
    if (os.getenv("MEM0_AUTH_ALLOW_LEGACY") or "1").strip() == "1":
        return True
    mode = (os.getenv("AUTH_MODE") or "warn").strip().lower()
    return mode in ("warn", "off")


def auth_headers_from_http_request(request) -> Optional[dict[str, str]]:
    """Extract registry credentials already present on an inbound HTTP request."""
    headers: dict[str, str] = {}
    authorization = request.headers.get("authorization")
    if authorization:
        headers["Authorization"] = authorization
    api_key = request.headers.get("x-api-key")
    if api_key:
        headers["X-API-Key"] = api_key
    if "Authorization" not in headers:
        token = request.query_params.get("token")
        if token:
            headers["Authorization"] = f"Bearer {token}"
    return headers or None


def synthesize_registry_auth_headers() -> Optional[dict[str, str]]:
    """Build AgentRegistry credentials from the OpenMemory auth context.

    MCP/memory calls often arrive without an ``Authorization`` header (hostname
    legacy URL, or Google session already validated by AuthMiddleware). The
    AgentRegistry sidecar still requires a bearer it understands — JWT,
    ``omtk_``, or ``Bearer local``. Re-issue or shim so the loja accepts the
    same principals OpenMemory already accepted.
    """
    from app.utils.logging_context import (
        auth_email_var,
        auth_method_var,
        auth_user_var,
        team_var,
    )
    from app.utils.session_jwt import SessionJwtError, issue_session_jwt

    method = (auth_method_var.get() or "").strip().lower()
    user_id = (auth_user_var.get() or "").strip()
    email = (auth_email_var.get() or "").strip()
    if not user_id and team_var.get():
        user_id = f"team:{team_var.get()}"

    if method in ("session", "agent_token", "team") and user_id:
        try:
            token = issue_session_jwt(user_id=user_id, email=email, name="")
            return {"Authorization": f"Bearer {token}"}
        except SessionJwtError:
            pass

    if legacy_registry_auth_allowed():
        return {"Authorization": "Bearer local"}
    return None


def resolve_registry_auth_headers(
    explicit: Optional[dict[str, str]] = None,
) -> Optional[dict[str, str]]:
    """Prefer caller-supplied headers, else synthesize from the Mem0 session."""
    if explicit:
        return explicit
    return synthesize_registry_auth_headers()
