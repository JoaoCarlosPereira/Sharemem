/**
 * Sanitização de headers do /api-proxy: remove hop-by-hop e content-length
 * antes de repassar ao upstream (fix do 500 atrás de nginx/Traefik).
 */
import {
  HOP_BY_HOP_HEADERS,
  applyLegacyAdminToken,
  buildUpstreamTarget,
  isUnsafePathSegment,
  rewriteUpstreamRedirectLocation,
  sanitizeUpstreamHeaders,
} from "@/lib/proxy-headers";

describe("sanitizeUpstreamHeaders", () => {
  it("remove todos os headers hop-by-hop e content-length", () => {
    const input = new Headers({
      host: "memorias.sysmo.com.br",
      connection: "upgrade",
      "keep-alive": "timeout=5",
      "transfer-encoding": "chunked",
      upgrade: "websocket",
      "content-length": "0",
      "proxy-connection": "keep-alive",
      te: "trailers",
    });
    const out = sanitizeUpstreamHeaders(input);
    for (const name of HOP_BY_HOP_HEADERS) {
      expect(out.has(name)).toBe(false);
    }
  });

  it("preserva headers de aplicação (Authorization, x-client-name, cookie)", () => {
    const input = new Headers({
      authorization: "Bearer jwt-abc",
      "x-client-name": "openmemory-ui",
      cookie: "session=1",
      connection: "keep-alive",
    });
    const out = sanitizeUpstreamHeaders(input);
    expect(out.get("authorization")).toBe("Bearer jwt-abc");
    expect(out.get("x-client-name")).toBe("openmemory-ui");
    expect(out.get("cookie")).toBe("session=1");
    expect(out.has("connection")).toBe(false);
  });

  it("não muta o objeto de entrada", () => {
    const input = new Headers({ connection: "close", authorization: "Bearer x" });
    sanitizeUpstreamHeaders(input);
    expect(input.get("connection")).toBe("close");
  });
});

describe("applyLegacyAdminToken", () => {
  const token = "test-admin-secret";

  it("injeta X-Admin-Token em POST /admin/* sem credencial", () => {
    const out = applyLegacyAdminToken(new Headers(), {
      method: "POST",
      pathSegments: ["admin", "backup", "run"],
      adminToken: token,
      legacyUi: true,
    });
    expect(out.get("x-admin-token")).toBe(token);
  });

  it("não injeta em GET", () => {
    const out = applyLegacyAdminToken(new Headers(), {
      method: "GET",
      pathSegments: ["admin", "backup", "status"],
      adminToken: token,
      legacyUi: true,
    });
    expect(out.has("x-admin-token")).toBe(false);
  });

  it("não sobrescreve Authorization existente (sessão JWT real)", () => {
    const input = new Headers({ authorization: "Bearer jwt-session" });
    const out = applyLegacyAdminToken(input, {
      method: "POST",
      pathSegments: ["admin", "backup", "run"],
      adminToken: token,
      legacyUi: true,
    });
    expect(out.has("x-admin-token")).toBe(false);
    expect(out.get("authorization")).toBe("Bearer jwt-session");
  });

  it("injeta X-Admin-Token quando chega só o shim legado Bearer local (fix 401 backup/restore)", () => {
    const input = new Headers({ authorization: "Bearer local" });
    const out = applyLegacyAdminToken(input, {
      method: "POST",
      pathSegments: ["admin", "backup", "restore"],
      adminToken: token,
      legacyUi: true,
    });
    expect(out.get("x-admin-token")).toBe(token);
    // o shim não é credencial: é removido, não vira Bearer no upstream
    expect(out.has("authorization")).toBe(false);
  });

  it("não injeta Bearer local em rotas fora de /admin", () => {
    const input = new Headers({ authorization: "Bearer local" });
    const out = applyLegacyAdminToken(input, {
      method: "POST",
      pathSegments: ["api", "v1", "memories"],
      adminToken: token,
      legacyUi: true,
    });
    expect(out.has("x-admin-token")).toBe(false);
    expect(out.get("authorization")).toBe("Bearer local");
  });

  it("não sobrescreve X-Admin-Token existente", () => {
    const input = new Headers({ "x-admin-token": "explicit" });
    const out = applyLegacyAdminToken(input, {
      method: "PUT",
      pathSegments: ["admin", "backup", "policy"],
      adminToken: token,
      legacyUi: true,
    });
    expect(out.get("x-admin-token")).toBe("explicit");
  });

  it("ignora rotas fora de /admin", () => {
    const out = applyLegacyAdminToken(new Headers(), {
      method: "POST",
      pathSegments: ["api", "v1", "memories"],
      adminToken: token,
      legacyUi: true,
    });
    expect(out.has("x-admin-token")).toBe(false);
  });

  it("modo Google (UI exige login): mutação /admin/* anônima NÃO recebe token", () => {
    for (const headers of [new Headers(), new Headers({ authorization: "Bearer local" })]) {
      const out = applyLegacyAdminToken(headers, {
        method: "POST",
        pathSegments: ["admin", "backup", "restore"],
        adminToken: token,
        legacyUi: false,
      });
      expect(out.has("x-admin-token")).toBe(false);
    }
  });

  describe("/api/v1/config (admin em todos os métodos)", () => {
    it.each(["GET", "PUT", "PATCH", "POST"])(
      "injeta X-Admin-Token em %s /api/v1/config/* sem credencial (UI legado)",
      (method) => {
        const out = applyLegacyAdminToken(new Headers(), {
          method,
          pathSegments: ["api", "v1", "config", "mem0", "llm"],
          adminToken: token,
          legacyUi: true,
        });
        expect(out.get("x-admin-token")).toBe(token);
      },
    );

    it("injeta no GET da raiz /api/v1/config e troca o shim Bearer local", () => {
      const out = applyLegacyAdminToken(
        new Headers({ authorization: "Bearer local" }),
        {
          method: "GET",
          pathSegments: ["api", "v1", "config"],
          adminToken: token,
          legacyUi: true,
        },
      );
      expect(out.get("x-admin-token")).toBe(token);
      expect(out.has("authorization")).toBe(false);
    });

    it("não injeta quando há sessão JWT real", () => {
      const out = applyLegacyAdminToken(
        new Headers({ authorization: "Bearer jwt-session" }),
        {
          method: "GET",
          pathSegments: ["api", "v1", "config"],
          adminToken: token,
          legacyUi: true,
        },
      );
      expect(out.has("x-admin-token")).toBe(false);
      expect(out.get("authorization")).toBe("Bearer jwt-session");
    });

    it("não injeta em config quando a UI exige login Google (fail-closed)", () => {
      const out = applyLegacyAdminToken(new Headers(), {
        method: "GET",
        pathSegments: ["api", "v1", "config"],
        adminToken: token,
        legacyUi: false,
      });
      expect(out.has("x-admin-token")).toBe(false);
    });

    it("não amplia para caminhos vizinhos (configs, outros /api/v1, GET /admin)", () => {
      for (const [method, pathSegments] of [
        ["GET", ["api", "v1", "configs"]],
        ["GET", ["api", "v1", "config-foo"]],
        ["GET", ["api", "v1", "memories"]],
        ["GET", ["api", "v2", "config"]],
        ["GET", ["admin", "backup", "status"]],
      ] as const) {
        const out = applyLegacyAdminToken(new Headers(), {
          method,
          pathSegments: [...pathSegments],
          adminToken: token,
          legacyUi: true,
        });
        expect(out.has("x-admin-token")).toBe(false);
      }
    });

    it("sem ADMIN_TOKEN configurado não injeta nada", () => {
      const out = applyLegacyAdminToken(new Headers(), {
        method: "GET",
        pathSegments: ["api", "v1", "config"],
        adminToken: "",
        legacyUi: true,
      });
      expect(out.has("x-admin-token")).toBe(false);
    });
  });
});

describe("rewriteUpstreamRedirectLocation", () => {
  const internal = "http://openmemory-mcp:8765";

  it("reescreve Location absoluta da API interna para /api-proxy", () => {
    expect(
      rewriteUpstreamRedirectLocation(
        "http://openmemory-mcp:8765/api/v1/apps/?page=1",
        internal,
      ),
    ).toBe("/api-proxy/api/v1/apps/?page=1");
  });

  it("reescreve path relativo do upstream", () => {
    expect(rewriteUpstreamRedirectLocation("/api/v1/apps/", internal)).toBe(
      "/api-proxy/api/v1/apps/",
    );
  });

  it("mantém Location externa inalterada", () => {
    const external = "https://accounts.google.com/o/oauth2/v2/auth";
    expect(rewriteUpstreamRedirectLocation(external, internal)).toBe(external);
  });
});

describe("buildUpstreamTarget (anti path traversal)", () => {
  const base = "http://openmemory-mcp:8765";

  it("monta a URL e devolve os segmentos do pathname final", () => {
    expect(buildUpstreamTarget(base, ["api", "v1", "config"], "?a=1")).toEqual({
      url: `${base}/api/v1/config?a=1`,
      pathSegments: ["api", "v1", "config"],
    });
  });

  it.each([
    [["admin", "../../api/v1/config"]],
    [["api", "v1", "config", "..", "..", "..", "admin"]],
    [["api", "v1", "config", "."]],
    [["admin", "..\\api"]],
    [["admin", "%2e%2e"]],
    [["admin", "a\u0000b"]],
    [["admin", ""]],
    [["admin", "a/../b"]],
    [["admin", "a//b"]],
    [["admin", "/a"]],
    [["admin", "a\\..\\b"]],
    [["admin", "a%2F..%2Fb"]],
  ])("rejeita %j", (segments) => {
    expect(buildUpstreamTarget(base, segments, "")).toBeNull();
  });

  it("isUnsafePathSegment aceita segmentos comuns", () => {
    for (const s of [
      "api",
      "v1",
      "meu projeto",
      "a.b",
      "...",
      "a?b",
      "user@x",
      "team/new-skill",
      "50%off",
      "100%",
      "a%zz",
    ]) {
      expect(isUnsafePathSegment(s)).toBe(false);
    }
  });

  it("segmento com '/' é achatado no path final (visão da API)", () => {
    expect(
      buildUpstreamTarget(
        base,
        ["api", "v1", "store", "skills", "team/new-skill", "latest"],
        "",
      ),
    ).toEqual({
      url: `${base}/api/v1/store/skills/team/new-skill/latest`,
      pathSegments: ["api", "v1", "store", "skills", "team", "new-skill", "latest"],
    });
  });

  it("'%' literal vira %25 (sem dupla decodificação)", () => {
    expect(
      buildUpstreamTarget(base, ["admin", "projects", "50%off"], "")?.url,
    ).toBe(`${base}/admin/projects/50%25off`);
  });
});
