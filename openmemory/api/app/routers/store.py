"""Store endpoints backed by AgentRegistry metadata."""

from __future__ import annotations

import os
import hashlib
import io
import zipfile
from typing import Literal, Optional

from app.services.store_recipes import (
    HOOK_ARTIFACT_MEDIA_TYPE,
    SKILL_ARTIFACT_MEDIA_TYPE,
    STORE_API_PREFIX,
    InstallRecipeService,
    StoreRecipeError,
)
from app.services.hook_packages import HookPackageInput
from app.services.skill_packages import SkillPackageInput
from app.utils.agentregistry import AgentRegistryHttpClient, AgentRegistryError
from app.utils.logging_context import auth_method_var, auth_user_var, team_var
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

router = APIRouter(prefix=STORE_API_PREFIX, tags=["store"])


class InstallRecipeRequest(BaseModel):
    kind: Literal["skill", "hook", "mcpserver", "prompt", "agent", "plugin"]
    name: str = Field(min_length=1, max_length=128)
    tag: str = Field(min_length=1, max_length=128)
    target: Literal["cursor", "claude", "codex"]


def get_install_recipe_service() -> InstallRecipeService:
    return InstallRecipeService()


def _current_actor_id() -> str:
    method = auth_method_var.get()
    if auth_user_var.get():
        return auth_user_var.get()
    if team_var.get():
        return f"team:{team_var.get()}"
    if method == "legacy":
        return "legacy"
    if not method and (os.getenv("AUTH_MODE", "warn").strip().lower() == "off"):
        return "auth-off"
    raise HTTPException(status_code=401, detail="credencial ausente")


def _registry_auth_headers(request: Request) -> Optional[dict[str, str]]:
    from app.utils.agentregistry import (
        auth_headers_from_http_request,
        resolve_registry_auth_headers,
    )

    return resolve_registry_auth_headers(auth_headers_from_http_request(request))


def get_registry_client() -> AgentRegistryHttpClient:
    return AgentRegistryHttpClient()


@router.put("/skills/{name:path}/{tag}")
async def publish_skill_package(
    name: str,
    tag: str,
    payload: SkillPackageInput,
    request: Request,
    client: AgentRegistryHttpClient = Depends(get_registry_client),
) -> dict:
    """Publish a complete Skill directory and its declarative metadata."""
    if payload.name != name or payload.tag != tag:
        raise HTTPException(status_code=422, detail="nome/tag do caminho não conferem com o payload")
    from app.services.skill_packages import build_skill_archive

    try:
        archive, inventory = build_skill_archive(payload)
        resource = {
            "apiVersion": "ar.dev/v1alpha1",
            "kind": "Skill",
            "metadata": {"name": name, "tag": tag},
            "spec": {
                "title": payload.title or name,
                "description": payload.description,
                "language": payload.language,
            },
        }
        auth_headers = _registry_auth_headers(request)
        apply_result = await client.apply_resource(resource=resource, auth_headers=auth_headers)
        artifact_result = await client.put_skill_artifact(
            name=name,
            tag=tag,
            archive=archive,
            auth_headers=auth_headers,
        )
        return {
            "resource": resource,
            "apply": apply_result,
            "artifact": {
                "size": len(archive),
                "sha256": hashlib.sha256(archive).hexdigest(),
                "files": inventory,
                "transport": artifact_result,
            },
        }
    except (ValueError, AgentRegistryError) as exc:
        status = getattr(exc, "status_code", 422)
        detail = getattr(exc, "detail", str(exc))
        raise HTTPException(status_code=status, detail=detail) from exc


@router.get("/skills/{name:path}/{tag}/download")
async def download_skill_package(
    name: str,
    tag: str,
    request: Request,
    client: AgentRegistryHttpClient = Depends(get_registry_client),
) -> Response:
    """Download the complete Skill directory as a ZIP file."""
    try:
        data, headers = await client.get_skill_download(
            name=name,
            tag=tag,
            auth_headers=_registry_auth_headers(request),
        )
    except AgentRegistryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": headers.get(
                "content-disposition", f'attachment; filename="{name}-{tag}.zip"'
            ),
            "X-Content-Type-Options": "nosniff",
            "X-Skill-SHA256": hashlib.sha256(data).hexdigest(),
        },
    )


@router.get("/skills/{name:path}/{tag}/artifact")
async def download_skill_artifact(
    name: str,
    tag: str,
    request: Request,
    client: AgentRegistryHttpClient = Depends(get_registry_client),
) -> Response:
    """Stream the immutable tar.gz artifact byte-for-byte.

    This is the endpoint referenced by install recipes: their
    ``verify_artifact_sha256`` step checks the published artifact digest, which
    only matches these bytes. The ``/download`` route rebuilds a ZIP and hashes
    differently, so it cannot serve that purpose.
    """
    try:
        data, headers = await client.get_skill_artifact(
            name=name,
            tag=tag,
            auth_headers=_registry_auth_headers(request),
        )
    except AgentRegistryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    passthrough = {
        "Content-Disposition": headers.get(
            "content-disposition", f'attachment; filename="{name}-{tag}.tar.gz"'
        ),
        "X-Content-Type-Options": "nosniff",
        "X-Skill-SHA256": hashlib.sha256(data).hexdigest(),
    }
    for header_name in ("digest", "etag"):
        value = headers.get(header_name)
        if value:
            passthrough[header_name.title()] = value
    return Response(
        content=data,
        media_type=SKILL_ARTIFACT_MEDIA_TYPE,
        headers=passthrough,
    )


@router.get("/skills/{name:path}/{tag}/files")
async def list_skill_package_files(
    name: str,
    tag: str,
    request: Request,
    client: AgentRegistryHttpClient = Depends(get_registry_client),
) -> dict:
    """Return the ZIP inventory for the UI file tree."""
    try:
        data, _ = await client.get_skill_download(
            name=name,
            tag=tag,
            auth_headers=_registry_auth_headers(request),
        )
    except AgentRegistryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        files = [
            {"path": info.filename, "size": info.file_size}
            for info in archive.infolist()
            if not info.is_dir()
        ]
    return {"name": name, "tag": tag, "sha256": hashlib.sha256(data).hexdigest(), "files": files}


@router.delete("/skills/{name:path}/{tag}")
async def delete_skill_package(
    name: str,
    tag: str,
    request: Request,
    client: AgentRegistryHttpClient = Depends(get_registry_client),
) -> dict:
    """Delete one Skill tag through AgentRegistry authorization."""
    try:
        result = await client.delete_resource(
            kind="skill",
            name=name,
            tag=tag,
            auth_headers=_registry_auth_headers(request),
        )
    except AgentRegistryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return {"deleted": True, "name": name, "tag": tag, "result": result}


@router.put("/hooks/{name:path}/{tag}")
async def publish_hook_package(
    name: str,
    tag: str,
    payload: HookPackageInput,
    request: Request,
    client: AgentRegistryHttpClient = Depends(get_registry_client),
) -> dict:
    """Publish a complete Hook: its event map plus the files it ships."""
    if payload.name != name or payload.tag != tag:
        raise HTTPException(status_code=422, detail="nome/tag do caminho não conferem com o payload")
    from app.services.hook_packages import build_hook_archive, validate_command_references

    try:
        validate_command_references(payload)
        archive, inventory = build_hook_archive(payload)
        resource = {
            "apiVersion": "ar.dev/v1alpha1",
            "kind": "Hook",
            "metadata": {"name": name, "tag": tag},
            "spec": {
                "title": payload.title or name,
                "description": payload.description,
                "language": payload.language,
                "events": payload.to_registry_events(),
            },
        }
        auth_headers = _registry_auth_headers(request)
        apply_result = await client.apply_resource(resource=resource, auth_headers=auth_headers)
        artifact_result = await client.put_package_artifact(
            kind="hook",
            name=name,
            tag=tag,
            archive=archive,
            auth_headers=auth_headers,
        )
        return {
            "resource": resource,
            "apply": apply_result,
            "artifact": {
                "size": len(archive),
                "sha256": hashlib.sha256(archive).hexdigest(),
                "files": inventory,
                "transport": artifact_result,
            },
        }
    except (ValueError, AgentRegistryError) as exc:
        status = getattr(exc, "status_code", 422)
        detail = getattr(exc, "detail", str(exc))
        raise HTTPException(status_code=status, detail=detail) from exc


@router.get("/hooks/{name:path}/{tag}/download")
async def download_hook_package(
    name: str,
    tag: str,
    request: Request,
    client: AgentRegistryHttpClient = Depends(get_registry_client),
) -> Response:
    """Download the complete Hook directory as a ZIP file."""
    return await _package_zip_response(
        client=client, kind="hook", name=name, tag=tag, request=request
    )


@router.get("/hooks/{name:path}/{tag}/artifact")
async def download_hook_artifact(
    name: str,
    tag: str,
    request: Request,
    client: AgentRegistryHttpClient = Depends(get_registry_client),
) -> Response:
    """Stream the immutable Hook tar.gz artifact byte-for-byte.

    Same contract as the Skill artifact route: install recipes verify
    ``verify_artifact_sha256`` against these exact bytes.
    """
    try:
        data, headers = await client.get_package_artifact(
            kind="hook",
            name=name,
            tag=tag,
            auth_headers=_registry_auth_headers(request),
        )
    except AgentRegistryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return _artifact_response(
        data=data,
        headers=headers,
        filename=f"{name}-{tag}.tar.gz",
        media_type=HOOK_ARTIFACT_MEDIA_TYPE,
    )


@router.get("/hooks/{name:path}/{tag}/files")
async def list_hook_package_files(
    name: str,
    tag: str,
    request: Request,
    client: AgentRegistryHttpClient = Depends(get_registry_client),
) -> dict:
    """Return the ZIP inventory for the UI file tree."""
    return await _package_file_inventory(
        client=client, kind="hook", name=name, tag=tag, request=request
    )


@router.delete("/hooks/{name:path}/{tag}")
async def delete_hook_package(
    name: str,
    tag: str,
    request: Request,
    client: AgentRegistryHttpClient = Depends(get_registry_client),
) -> dict:
    """Delete one Hook tag through AgentRegistry authorization."""
    try:
        result = await client.delete_resource(
            kind="hook",
            name=name,
            tag=tag,
            auth_headers=_registry_auth_headers(request),
        )
    except AgentRegistryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return {"deleted": True, "name": name, "tag": tag, "result": result}


async def _package_zip_response(
    *,
    client: AgentRegistryHttpClient,
    kind: str,
    name: str,
    tag: str,
    request: Request,
) -> Response:
    try:
        data, headers = await client.get_package_download(
            kind=kind,
            name=name,
            tag=tag,
            auth_headers=_registry_auth_headers(request),
        )
    except AgentRegistryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": headers.get(
                "content-disposition", f'attachment; filename="{name}-{tag}.zip"'
            ),
            "X-Content-Type-Options": "nosniff",
            "X-Skill-SHA256": hashlib.sha256(data).hexdigest(),
        },
    )


async def _package_file_inventory(
    *,
    client: AgentRegistryHttpClient,
    kind: str,
    name: str,
    tag: str,
    request: Request,
) -> dict:
    try:
        data, _ = await client.get_package_download(
            kind=kind,
            name=name,
            tag=tag,
            auth_headers=_registry_auth_headers(request),
        )
    except AgentRegistryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        files = [
            {"path": info.filename, "size": info.file_size}
            for info in archive.infolist()
            if not info.is_dir()
        ]
    return {"name": name, "tag": tag, "sha256": hashlib.sha256(data).hexdigest(), "files": files}


def _artifact_response(
    *,
    data: bytes,
    headers: dict,
    filename: str,
    media_type: str,
) -> Response:
    passthrough = {
        "Content-Disposition": headers.get(
            "content-disposition", f'attachment; filename="{filename}"'
        ),
        "X-Content-Type-Options": "nosniff",
        "X-Skill-SHA256": hashlib.sha256(data).hexdigest(),
    }
    for header_name in ("digest", "etag"):
        value = headers.get(header_name)
        if value:
            passthrough[header_name.title()] = value
    return Response(content=data, media_type=media_type, headers=passthrough)


@router.post("/install-recipes")
async def build_install_recipe(
    payload: InstallRecipeRequest,
    request: Request,
    service: InstallRecipeService = Depends(get_install_recipe_service),
) -> dict:
    """Return a host-applied install recipe for a catalog resource."""
    try:
        return await service.build(
            kind=payload.kind,
            name=payload.name,
            tag=payload.tag,
            target=payload.target,
            user_id=_current_actor_id(),
            auth_headers=_registry_auth_headers(request),
        )
    except StoreRecipeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
