import { isLegacyAuthUi } from "@/lib/auth-ui-mode";
import { LEGACY_MCP_AUTH_HEADER } from "@/lib/mcp-install";

const REGISTRY_CATALOG_RESOURCES = new Set([
  "agents",
  "hooks",
  "mcpservers",
  "skills",
  "prompts",
  "plugins",
  "models",
]);
const REGISTRY_PACKAGE_RESOURCES = new Set(["skills", "hooks", "plugins"]);
const REGISTRY_PACKAGE_READ_ENDPOINTS = new Set(["artifact", "download", "files"]);

function normalizeSegments(pathSegments: string[]): string[] {
  return pathSegments.map((segment) => segment.trim()).filter(Boolean);
}

export function registryInternalBase(): string {
  return (
    process.env.AGENT_REGISTRY_INTERNAL_URL ||
    process.env.REGISTRY_INTERNAL_URL ||
    "http://agentregistry:8080"
  ).replace(/\/$/, "");
}

export function hasRegistryAuthorization(headers: Headers): boolean {
  const value = headers.get("authorization")?.trim();
  return !!value && /^Bearer\s+\S+\s*$/i.test(value);
}

/**
 * Em UI legado (AUTH_UI_REQUIRED=0 / sem Google), a sessão NextAuth não
 * carrega JWT — o browser chama /registry-api sem Authorization. O proxy
 * injeta ``Bearer local`` (mesmo shim do MCP) só nesse modo, alinhado a
 * ``MEM0_AUTH_ALLOW_LEGACY`` no agentregistry.
 *
 * Com Google auth exigido, não injeta nada: permanece fail-closed até o
 * axios enviar o JWT da sessão.
 */
export function applyLegacyRegistryAuthorization(headers: Headers): Headers {
  if (hasRegistryAuthorization(headers)) {
    return headers;
  }
  if (!isLegacyAuthUi()) {
    return headers;
  }
  const out = new Headers(headers);
  out.set("authorization", LEGACY_MCP_AUTH_HEADER);
  return out;
}

export function isRegistryProxyPathAllowed(
  method: string,
  pathSegments: string[],
): boolean {
  const normalizedMethod = method.toUpperCase();
  const segments = normalizeSegments(pathSegments);

  if (normalizedMethod === "GET") {
    return (
      isNativeCatalogReadAllowed(segments) ||
      isMcpRegistryCompatReadAllowed(segments)
    );
  }

  if (normalizedMethod === "POST") {
    return segments.length === 2 && segments[0] === "v0" && segments[1] === "apply";
  }

  if (normalizedMethod === "PUT") {
    return (
      segments.length === 5 &&
      segments[0] === "v0" &&
      segments[1] === "skills" &&
      segments[4] === "artifact"
    );
  }

  if (normalizedMethod === "DELETE") {
    return (
      segments.length === 4 &&
      segments[0] === "v0" &&
      REGISTRY_PACKAGE_RESOURCES.has(segments[1])
    );
  }

  return false;
}

export function registryProxyTarget(
  baseUrl: string,
  pathSegments: string[],
  search: string,
): string {
  const suffix = normalizeSegments(pathSegments)
    .map((segment) => encodeURIComponent(segment))
    .join("/");
  return `${baseUrl}${suffix ? `/${suffix}` : ""}${search}`;
}

function isNativeCatalogReadAllowed(pathSegments: string[]): boolean {
  if (pathSegments.length < 2 || pathSegments.length > 5) return false;
  if (pathSegments[0] !== "v0") return false;
  if (!REGISTRY_CATALOG_RESOURCES.has(pathSegments[1])) return false;
  if (pathSegments.length === 5) {
    return (
      REGISTRY_PACKAGE_RESOURCES.has(pathSegments[1]) &&
      REGISTRY_PACKAGE_READ_ENDPOINTS.has(pathSegments[4])
    );
  }
  return true;
}

function isMcpRegistryCompatReadAllowed(pathSegments: string[]): boolean {
  if (pathSegments[0] !== "v0.1" || pathSegments[1] !== "servers") {
    return false;
  }
  if (pathSegments.length === 2) return true;
  if (pathSegments.length === 4 && pathSegments[3] === "versions") return true;
  if (pathSegments.length === 5 && pathSegments[3] === "versions") return true;
  return false;
}
