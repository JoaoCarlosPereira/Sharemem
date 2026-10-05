import { isLegacyAuthUi } from "@/lib/auth-ui-mode";

/**
 * Sanitização de headers do reverse proxy /api-proxy.
 *
 * Remove headers hop-by-hop (RFC 7230 §6.1) e os que o fetch/undici recalcula
 * antes de repassar a requisição do cliente para a API interna. Sem isso, com
 * a UI atrás de um proxy (nginx/Traefik), headers normalizados (Connection,
 * Transfer-Encoding, Content-Length) fazem o fetch do upstream lançar → 500.
 */
export const HOP_BY_HOP_HEADERS = [
  "host",
  "connection",
  "keep-alive",
  "proxy-connection",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
  "content-length",
];

export function sanitizeUpstreamHeaders(input: Headers): Headers {
  const out = new Headers(input);
  for (const name of HOP_BY_HOP_HEADERS) out.delete(name);
  return out;
}

const MUTATING_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

const LEGACY_LOCAL_BEARER = "bearer local";

/**
 * O shim legado ``Bearer local`` (UI sem sessão Google, ver ``AuthBridge`` /
 * ``api-client``) NÃO é credencial para ``/admin/*``: o backend o rejeita no
 * ``require_admin``. Só um Bearer real (sessão JWT) ou um X-Admin-Token
 * explícito contam como credencial aqui.
 */
function hasAdminCredential(headers: Headers): boolean {
  if (headers.get("x-admin-token")?.trim()) return true;
  const auth = (headers.get("authorization") ?? "").trim();
  return auth.length > 0 && auth.toLowerCase() !== LEGACY_LOCAL_BEARER;
}

/**
 * ``/api/v1/config/*`` é inteiramente admin (inclusive GET: devolve a config do
 * LLM). Casa só o prefixo exato ``api/v1/config`` — não ``configs``,
 * ``config-foo`` etc.
 */
function isConfigPath(segments: string[]): boolean {
  return (
    segments[0] === "api" && segments[1] === "v1" && segments[2] === "config"
  );
}

/** GETs admin-only consumidos pela tela de governança (propostas de merge). */
function isAdminGovernanceReadPath(segments: string[]): boolean {
  return (
    segments[0] === "admin" &&
    segments[1] === "governance" &&
    segments[2] === "projects" &&
    ["merge-proposals", "merge-inconsistencies", "merge-preview"].includes(
      segments[3],
    )
  );
}

/** ``POST /api/v1/apps/{id}/rename`` exige admin (pode virar proposta de merge). */
function isAppRenamePath(segments: string[]): boolean {
  return (
    segments.length === 5 &&
    segments[0] === "api" &&
    segments[1] === "v1" &&
    segments[2] === "apps" &&
    segments[4] === "rename"
  );
}

function needsLegacyAdminToken(
  method: string,
  segments: string[],
  legacyUi: boolean,
): boolean {
  // SÓ em UI legado. Com login Google exigido as telas admin mandam o Bearer
  // da sessão (interceptor do axios em ``api-client``); injetar aqui daria a
  // qualquer anônimo que alcance a UI (``/api-proxy`` fica fora do middleware
  // de login) poderes admin — leitura/escrita da config do LLM, restore etc.
  if (!legacyUi) return false;
  // Config: todos os métodos (o GET devolve a config do LLM).
  if (isConfigPath(segments)) return true;
  const verb = method.toUpperCase();
  if (verb === "GET" && isAdminGovernanceReadPath(segments)) return true;
  if (verb === "POST" && isAppRenamePath(segments)) return true;
  return segments[0] === "admin" && MUTATING_METHODS.has(verb);
}

/**
 * Em UI legado (sem sessão Google), mutações ``/admin/*`` e QUALQUER chamada a
 * ``/api/v1/config/*`` exigem ``X-Admin-Token`` / sessão JWT. O browser não deve
 * receber ``ADMIN_TOKEN``; o proxy injeta o segredo server-side quando a
 * chamada chega sem credencial.
 *
 * A injeção só ocorre em UI legado (``isLegacyAuthUi``); ``legacyUi`` permite
 * sobrescrever em testes. ``pathSegments`` DEVE vir do pathname upstream final
 * já normalizado (ver :func:`buildUpstreamTarget`), nunca dos params crus.
 *
 * Não sobrescreve Authorization (sessão JWT) / X-Admin-Token já enviados.
 * O shim legado ``Bearer local`` é tratado como ausência de credencial: é
 * removido e substituído por ``X-Admin-Token`` — senão as mutações de
 * backup/restore falham com 401 em modo legado.
 */
export function applyLegacyAdminToken(
  headers: Headers,
  options: {
    method: string;
    pathSegments: string[];
    adminToken?: string;
    legacyUi?: boolean;
  },
): Headers {
  const token = (options.adminToken ?? process.env.ADMIN_TOKEN ?? "").trim();
  if (!token) return headers;
  const legacyUi = options.legacyUi ?? isLegacyAuthUi();
  if (!needsLegacyAdminToken(options.method, options.pathSegments, legacyUi)) {
    return headers;
  }
  if (hasAdminCredential(headers)) return headers;

  const out = new Headers(headers);
  out.delete("authorization");
  out.set("x-admin-token", token);
  return out;
}

/** Separadores que o parser WHATWG trata como ``/`` em URLs http(s). */
const PATH_SEPARATORS = /[/\\]/;

function hasDotOrEmptyPiece(value: string): boolean {
  return value
    .split(PATH_SEPARATORS)
    .some((p) => p === "" || p === "." || p === "..");
}

/**
 * Segmento inseguro para repassar ao upstream. O Next entrega os segmentos de
 * ``[...path]`` já decodificados (``%2f`` → ``/``, ``%2e`` → ``.``): sem esta
 * checagem, ``admin/..%2f..%2fapi%2fv1%2fconfig`` é visto como ``admin/*`` mas o
 * fetch resolve ``..`` e chega em ``/api/v1/config``.
 *
 * Um segmento decodificado PODE conter ``/`` (ex.: nome de skill
 * ``team/new-skill`` publicado como ``team%2Fnew-skill`` e aceito pela API via
 * ``{name:path}``) e ``%`` (``50%25off`` → ``50%off``). Rejeita só quando,
 * dividido por ``/`` ou ``\``, algum pedaço é ``""``, ``.`` ou ``..``; também
 * rejeita caracteres de controle e, por defesa em profundidade, ``.``/``..``
 * duplamente codificados (``%252e%252e`` → ``%2e%2e``), caso algum hop
 * intermediário decodifique de novo.
 */
export function isUnsafePathSegment(segment: string): boolean {
  if (hasDotOrEmptyPiece(segment)) return true;
  // eslint-disable-next-line no-control-regex
  if (/[\u0000-\u001f\u007f]/.test(segment)) return true;
  if (segment.includes("%")) {
    let again: string;
    try {
      again = decodeURIComponent(segment);
    } catch {
      return false; // ``%`` literal sem escape válido: vira ``%25`` adiante
    }
    if (again !== segment) {
      if (
        again
          .split(PATH_SEPARATORS)
          .some((p) => p === "." || p === "..")
      ) {
        return true;
      }
      // eslint-disable-next-line no-control-regex
      if (/[\u0000-\u001f\u007f]/.test(again)) return true;
    }
  }
  return false;
}

export type UpstreamTarget = {
  /** URL absoluta a passar para ``fetch``. */
  url: string;
  /**
   * Segmentos (decodificados) do pathname FINAL relativo à base — é isto que
   * decide a injeção de credencial, não os params crus do Next.
   */
  pathSegments: string[];
};

/**
 * Monta a URL upstream do /api-proxy de forma segura (defesa em profundidade):
 *
 * 1. rejeita qualquer segmento inseguro (:func:`isUnsafePathSegment`);
 * 2. divide segmentos por ``/`` decodificado (visão da API) e codifica cada
 *    pedaço (``?``/``#``/``%`` decodificados não viram query/fragment/escape);
 * 3. resolve com ``new URL()`` e confere que o pathname resultante continua sob
 *    o path da base; os segmentos devolvidos vêm desse pathname normalizado.
 *
 * Retorna ``null`` se o caminho for inválido (o handler responde 400).
 */
export function buildUpstreamTarget(
  internalBase: string,
  segments: string[],
  search: string,
): UpstreamTarget | null {
  if (segments.some(isUnsafePathSegment)) return null;
  let base: URL;
  try {
    base = new URL(internalBase);
  } catch {
    return null;
  }
  const basePath = base.pathname.replace(/\/+$/, "");
  // Visão do servidor: um ``/`` decodificado dentro de um segmento é separador
  // de path para a API (Starlette decodifica ``%2F`` antes do roteamento). Por
  // isso achatamos os segmentos em pedaços e repassamos ``/`` literal — mesmo
  // upstream de antes (``team%2Fnew-skill`` → ``.../skills/team/new-skill/...``,
  // resolvido por ``{name:path}``) e, sobretudo, a decisão de injeção passa a
  // olhar exatamente o path que a API vai rotear.
  const pieces = segments.flatMap((s) => s.split("/"));
  const suffix = pieces.map((s) => encodeURIComponent(s)).join("/");
  let target: URL;
  try {
    target = new URL(`${base.origin}${basePath}/${suffix}${search}`);
  } catch {
    return null;
  }
  if (target.origin !== base.origin) return null;
  const prefix = `${basePath}/`;
  if (!target.pathname.startsWith(prefix) && target.pathname !== basePath) {
    return null;
  }
  const rest = target.pathname.slice(prefix.length);
  let finalSegments: string[];
  try {
    finalSegments = rest ? rest.split("/").map((s) => decodeURIComponent(s)) : [];
  } catch {
    return null;
  }
  // O pathname normalizado precisa bater 1:1 com o pedido (nenhum ``..``
  // resolvido, nenhum segmento perdido/adicionado).
  if (
    finalSegments.length !== pieces.length ||
    finalSegments.some((s, i) => s !== pieces[i])
  ) {
    return null;
  }
  return { url: target.toString(), pathSegments: finalSegments };
}

/**
 * Rewrite upstream redirect targets (Docker-internal API URL) to same-origin
 * ``/api-proxy`` so the browser never follows ``openmemory-mcp:8765``.
 */
export function rewriteUpstreamRedirectLocation(
  location: string,
  internalBase: string,
): string {
  const base = internalBase.replace(/\/$/, "");
  if (!location) {
    return location;
  }

  if (location.startsWith(base)) {
    return `/api-proxy${location.slice(base.length)}`;
  }

  try {
    const parsed = new URL(location);
    const internal = new URL(base.includes("://") ? base : `http://${base}`);
    if (parsed.host === internal.host) {
      return `/api-proxy${parsed.pathname}${parsed.search}${parsed.hash}`;
    }
  } catch {
    if (location.startsWith("/")) {
      return `/api-proxy${location}`;
    }
  }

  return location;
}
