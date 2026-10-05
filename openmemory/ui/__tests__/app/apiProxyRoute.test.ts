/**
 * Route handler do /api-proxy (não só o helper): URL upstream e headers.
 *
 * Regressão B1: o Next decodifica ``%2f`` dentro de cada segmento; a decisão de
 * injetar ``X-Admin-Token`` olhava os segmentos decodificados, mas o fetch
 * upstream resolvia ``..`` — ``PUT /api-proxy/admin/..%2f..%2fapi%2fv1%2fconfig``
 * chegava como ``PUT /api/v1/config`` COM o token admin (e
 * ``GET /api-proxy/api/v1/config/..%2f..%2f..%2fadmin%2fwrite-queue`` levava o
 * token a ``GET /admin/*``).
 */
import type { NextRequest } from "next/server";

import { DELETE, GET, PATCH, POST, PUT } from "@/app/api-proxy/[...path]/route";

jest.mock("next/server", () => {
  class MockNextResponse {
    body: BodyInit | null;
    headers: Headers;
    status: number;

    constructor(body?: BodyInit | null, init?: ResponseInit) {
      this.body = body ?? null;
      this.headers = new Headers(init?.headers);
      this.status = init?.status ?? 200;
    }

    static json(body: unknown, init?: ResponseInit) {
      const headers = new Headers(init?.headers);
      if (!headers.has("content-type")) {
        headers.set("content-type", "application/json");
      }
      return new MockNextResponse(JSON.stringify(body), { ...init, headers });
    }

    async json() {
      return JSON.parse(await this.text());
    }

    async text() {
      if (typeof this.body === "string") return this.body;
      if (this.body instanceof ArrayBuffer) {
        return Buffer.from(this.body).toString("utf8");
      }
      return "";
    }
  }

  return { NextResponse: MockNextResponse };
});

const HANDLERS = { GET, POST, PUT, PATCH, DELETE } as const;
type Method = keyof typeof HANDLERS;

const ADMIN = "proxy-admin-secret";
const BASE = "http://openmemory-mcp:8765";

/**
 * Simula o que o Next entrega ao handler: a URL crua e os segmentos já
 * decodificados UMA vez (``%2f`` → ``/``, ``%2e`` → ``.``).
 */
function call(
  method: Method,
  rawPath: string,
  options: { headers?: HeadersInit; search?: string } = {},
) {
  const segments = rawPath
    .split("/")
    .filter((s) => s.length > 0)
    .map((s) => decodeURIComponent(s));
  const req = {
    method,
    headers: new Headers(options.headers),
    nextUrl: new URL(`http://ui.local/api-proxy/${rawPath}${options.search ?? ""}`),
    arrayBuffer: async () => new ArrayBuffer(0),
  } as unknown as NextRequest;
  return HANDLERS[method](req, { params: Promise.resolve({ path: segments }) });
}

function lastFetch(): { url: string; headers: Headers } {
  const calls = (global.fetch as jest.Mock).mock.calls;
  const [url, init] = calls[calls.length - 1] as [string, RequestInit];
  return { url, headers: init.headers as Headers };
}

describe("/api-proxy route handler", () => {
  const saved = {
    ADMIN_TOKEN: process.env.ADMIN_TOKEN,
    API_INTERNAL_URL: process.env.API_INTERNAL_URL,
    AUTH_UI_REQUIRED: process.env.AUTH_UI_REQUIRED,
  };
  const originalFetch = global.fetch;

  beforeEach(() => {
    const { NextResponse } = jest.requireMock("next/server");
    process.env.ADMIN_TOKEN = ADMIN;
    process.env.API_INTERNAL_URL = BASE;
    process.env.AUTH_UI_REQUIRED = "0"; // legado por padrão
    global.fetch = jest
      .fn()
      .mockImplementation(async () => new NextResponse("{}", { status: 200 }));
  });

  afterEach(() => {
    for (const [k, v] of Object.entries(saved)) {
      if (v === undefined) delete process.env[k];
      else process.env[k] = v;
    }
    global.fetch = originalFetch;
  });

  describe("path traversal é rejeitado com 400 e nunca chega ao upstream", () => {
    it.each<[Method, string, string]>([
      ["PUT", "admin/..%2f..%2fapi%2fv1%2fconfig", "1"],
      ["PUT", "admin/..%2f..%2fapi%2fv1%2fconfig", "0"],
      ["GET", "api/v1/config/..%2f..%2f..%2fadmin%2fwrite-queue", "0"],
      ["GET", "api/v1/config/%2e%2e/%2e%2e/%2e%2e/admin/write-queue", "0"],
      ["POST", "api/v1/memories/..%5c..%5cadmin%5cbackup%5crestore", "0"],
      ["GET", "api/v1/config/./mem0", "0"],
      ["GET", "api/v1/config/%2e/mem0", "0"],
      ["GET", "api/v1/config/..", "0"],
      ["GET", "api/v1/config/%252e%252e/admin", "0"],
      ["PUT", "api/v1/store/skills/a%2F..%2F..%2F..%2Fconfig/latest", "0"],
      ["PUT", "api/v1/store/skills/a%2F..%2Fadmin/latest", "1"],
      ["GET", "api/v1/memories/a%2F%2Fb", "0"],
      ["GET", "api/v1/memories/a%2F", "0"],
      ["GET", "api/v1/memories/a%2F.%2Fb", "0"],
    ])("%s /api-proxy/%s (AUTH_UI_REQUIRED=%s)", async (method, rawPath, uiRequired) => {
      process.env.AUTH_UI_REQUIRED = uiRequired;
      const res = await call(method, rawPath);
      expect(res.status).toBe(400);
      expect(global.fetch).not.toHaveBeenCalled();
    });
  });

  describe("segmentos legítimos com '/' ou '%' (regressão do achado bloqueante)", () => {
    // Antes do card o proxy fazia ``${base}/${segments.join("/")}`` com os
    // segmentos já decodificados: ``team%2Fnew-skill`` chegava à API como
    // ``.../skills/team/new-skill/latest`` e ``{name:path}`` resolvia
    // ``name="team/new-skill"``. O upstream precisa continuar idêntico.
    it.each(["0", "1"])(
      "PUT store skill com barra no nome (AUTH_UI_REQUIRED=%s)",
      async (uiRequired) => {
        process.env.AUTH_UI_REQUIRED = uiRequired;
        const res = await call(
          "PUT",
          "api/v1/store/skills/team%2Fnew-skill/latest",
          { headers: { authorization: "Bearer jwt-sessao" } },
        );
        expect(res.status).toBe(200);
        const { url, headers } = lastFetch();
        expect(url).toBe(`${BASE}/api/v1/store/skills/team/new-skill/latest`);
        expect(headers.get("authorization")).toBe("Bearer jwt-sessao");
        // Store não é config nem admin: nunca recebe o token.
        expect(headers.has("x-admin-token")).toBe(false);
      },
    );

    it("PUT store hook com barra no nome, anônimo em modo legado: sem token", async () => {
      await call("PUT", "api/v1/store/hooks/team%2Fmy-hook/v1");
      const { url, headers } = lastFetch();
      expect(url).toBe(`${BASE}/api/v1/store/hooks/team/my-hook/v1`);
      expect(headers.has("x-admin-token")).toBe(false);
    });

    it("GET memórias com barra no segmento é repassado como antes", async () => {
      await call("GET", "api/v1/memories/a%2fb");
      expect(lastFetch().url).toBe(`${BASE}/api/v1/memories/a/b`);
    });

    it("projeto com barra: admin/projects/foo%2Fbar/memories", async () => {
      await call("GET", "admin/projects/foo%2Fbar/memories");
      const { url, headers } = lastFetch();
      expect(url).toBe(`${BASE}/admin/projects/foo/bar/memories`);
      expect(headers.has("x-admin-token")).toBe(false); // GET admin: sem token
    });

    it.each(["0", "1"])(
      "projeto com '%%': admin/projects/50%%25off/memories (AUTH_UI_REQUIRED=%s)",
      async (uiRequired) => {
        process.env.AUTH_UI_REQUIRED = uiRequired;
        const res = await call("GET", "admin/projects/50%25off/memories");
        expect(res.status).toBe(200);
        // ``%`` vira ``%25`` (sem dupla decodificação no upstream).
        expect(lastFetch().url).toBe(`${BASE}/admin/projects/50%25off/memories`);
      },
    );

    it("mutação admin com '%' no projeto em modo legado recebe token (destino é admin/*)", async () => {
      await call("POST", "admin/projects/50%25off/memories");
      const { url, headers } = lastFetch();
      expect(url).toBe(`${BASE}/admin/projects/50%25off/memories`);
      expect(headers.get("x-admin-token")).toBe(ADMIN);
    });

    it("decisão de injeção usa o destino final: 'api%2Fv1%2Fconfig' em um segmento", async () => {
      // Um único segmento decodificado ``api/v1/config`` vira o path
      // ``/api/v1/config`` no upstream — é config, então em legado recebe o
      // token (coerente com o destino real), e em modo Google não.
      await call("GET", "api%2Fv1%2Fconfig");
      expect(lastFetch().url).toBe(`${BASE}/api/v1/config`);
      expect(lastFetch().headers.get("x-admin-token")).toBe(ADMIN);

      process.env.AUTH_UI_REQUIRED = "1";
      await call("GET", "api%2Fv1%2Fconfig");
      expect(lastFetch().headers.has("x-admin-token")).toBe(false);
    });

    it("'admin%2Fbackup' disfarçado em segmento de store não vira atalho: destino é store", async () => {
      await call("POST", "api/v1/store/skills/admin%2Fbackup%2Frestore/latest");
      const { url, headers } = lastFetch();
      expect(url).toBe(`${BASE}/api/v1/store/skills/admin/backup/restore/latest`);
      expect(headers.has("x-admin-token")).toBe(false);
    });
  });

  it("modo Google, anônimo: PUT /api/v1/config não recebe token admin", async () => {
    process.env.AUTH_UI_REQUIRED = "1";
    await call("PUT", "api/v1/config");
    const { url, headers } = lastFetch();
    expect(url).toBe(`${BASE}/api/v1/config`);
    expect(headers.has("x-admin-token")).toBe(false);
  });

  it("modo Google, anônimo: mutação /admin/* não recebe token admin", async () => {
    process.env.AUTH_UI_REQUIRED = "1";
    await call("POST", "admin/backup/restore");
    const { url, headers } = lastFetch();
    expect(url).toBe(`${BASE}/admin/backup/restore`);
    expect(headers.has("x-admin-token")).toBe(false);
  });

  it("modo Google, sessão: repassa o Bearer da sessão sem injetar o token", async () => {
    process.env.AUTH_UI_REQUIRED = "1";
    await call("POST", "admin/backup/run", {
      headers: { authorization: "Bearer jwt-sessao" },
    });
    const { headers } = lastFetch();
    expect(headers.get("authorization")).toBe("Bearer jwt-sessao");
    expect(headers.has("x-admin-token")).toBe(false);
  });

  it("modo legado: GET /api/v1/config recebe o token", async () => {
    await call("GET", "api/v1/config", { search: "?x=1" });
    const { url, headers } = lastFetch();
    expect(url).toBe(`${BASE}/api/v1/config?x=1`);
    expect(headers.get("x-admin-token")).toBe(ADMIN);
  });

  it("modo legado: PUT /api/v1/config/mem0/llm recebe o token", async () => {
    await call("PUT", "api/v1/config/mem0/llm");
    const { url, headers } = lastFetch();
    expect(url).toBe(`${BASE}/api/v1/config/mem0/llm`);
    expect(headers.get("x-admin-token")).toBe(ADMIN);
  });

  it("modo legado: POST /admin/backup/run troca Bearer local pelo token", async () => {
    await call("POST", "admin/backup/run", {
      headers: { authorization: "Bearer local" },
    });
    const { headers } = lastFetch();
    expect(headers.get("x-admin-token")).toBe(ADMIN);
    expect(headers.has("authorization")).toBe(false);
  });

  it("modo legado: GET /admin/* não recebe o token", async () => {
    await call("GET", "admin/write-queue");
    expect(lastFetch().headers.has("x-admin-token")).toBe(false);
  });

  it.each<[Method, string]>([
    ["GET", "API/v1/config"],
    ["GET", "api/V1/Config"],
    ["POST", "Admin/backup/restore"],
    ["GET", "api/v1/configs"],
    ["GET", "api/v1/memories"],
  ])("modo legado: %s /%s (maiúsculas/vizinhos) não recebe o token", async (method, rawPath) => {
    await call(method, rawPath);
    const { url, headers } = lastFetch();
    expect(url).toBe(`${BASE}/${rawPath}`);
    expect(headers.has("x-admin-token")).toBe(false);
  });

  it("codifica segmentos: '?' e '#' decodificados não viram query/fragment", async () => {
    await call("GET", "api/v1/memories/a%3Fb%23c");
    const { url } = lastFetch();
    expect(url).toBe(`${BASE}/api/v1/memories/a%3Fb%23c`);
  });

  it("caminho legítimo com espaço/@ é repassado codificado", async () => {
    await call("GET", "admin/projects/meu%20projeto/memories");
    expect(lastFetch().url).toBe(`${BASE}/admin/projects/meu%20projeto/memories`);
  });

  it("API_INTERNAL_URL com prefixo de path é preservado", async () => {
    process.env.API_INTERNAL_URL = "http://gw:8080/om/";
    await call("GET", "api/v1/config");
    const { url, headers } = lastFetch();
    expect(url).toBe("http://gw:8080/om/api/v1/config");
    expect(headers.get("x-admin-token")).toBe(ADMIN);
  });
});
