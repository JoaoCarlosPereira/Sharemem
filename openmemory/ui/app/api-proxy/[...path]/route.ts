import { NextRequest, NextResponse } from "next/server";

import {
  applyLegacyAdminToken,
  buildUpstreamTarget,
  rewriteUpstreamRedirectLocation,
  sanitizeUpstreamHeaders,
} from "@/lib/proxy-headers";

function internalBase(): string {
  return (process.env.API_INTERNAL_URL || "http://localhost:8765").replace(
    /\/$/,
    "",
  );
}

async function proxyRequest(
  req: NextRequest,
  pathSegments: string[],
): Promise<NextResponse> {
  // Segmentos chegam decodificados pelo Next: valida e normaliza ANTES de
  // decidir qualquer injeção de credencial (anti path traversal ``..%2f``).
  const upstreamTarget = buildUpstreamTarget(
    internalBase(),
    pathSegments ?? [],
    req.nextUrl.search,
  );
  if (!upstreamTarget) {
    return NextResponse.json({ detail: "caminho inválido" }, { status: 400 });
  }
  const target = upstreamTarget.url;

  const headers = applyLegacyAdminToken(sanitizeUpstreamHeaders(req.headers), {
    method: req.method,
    pathSegments: upstreamTarget.pathSegments,
  });

  const hasBody = req.method !== "GET" && req.method !== "HEAD";
  // Follow redirects server-side for GET/HEAD: FastAPI trailing-slash 307s point at
  // API_INTERNAL_URL (openmemory-mcp:8765), which the browser cannot reach (CSP).
  const followRedirects = req.method === "GET" || req.method === "HEAD";
  const init: RequestInit = {
    method: req.method,
    headers,
    redirect: followRedirects ? "follow" : "manual",
  };
  if (hasBody) {
    init.body = await req.arrayBuffer();
  }

  let upstream: Response;
  try {
    upstream = await fetch(target, init);
  } catch (err) {
    // Falha de conexão/transporte com a API interna: responde JSON 502 em vez
    // de deixar o handler estourar (que viraria uma página HTML 500).
    return NextResponse.json(
      { detail: "upstream indisponível", error: String(err) },
      { status: 502 },
    );
  }

  const responseHeaders = new Headers(upstream.headers);
  if (
    !followRedirects &&
    upstream.status >= 300 &&
    upstream.status < 400
  ) {
    const location = responseHeaders.get("location");
    if (location) {
      responseHeaders.set(
        "location",
        rewriteUpstreamRedirectLocation(location, internalBase()),
      );
    }
  }
  responseHeaders.delete("transfer-encoding");
  responseHeaders.delete("content-encoding");
  responseHeaders.delete("content-length");

  return new NextResponse(upstream.body, {
    status: upstream.status,
    headers: responseHeaders,
  });
}

type RouteContext = { params: Promise<{ path: string[] }> };

export async function GET(req: NextRequest, ctx: RouteContext) {
  const { path } = await ctx.params;
  return proxyRequest(req, path);
}

export async function POST(req: NextRequest, ctx: RouteContext) {
  const { path } = await ctx.params;
  return proxyRequest(req, path);
}

export async function PUT(req: NextRequest, ctx: RouteContext) {
  const { path } = await ctx.params;
  return proxyRequest(req, path);
}

export async function PATCH(req: NextRequest, ctx: RouteContext) {
  const { path } = await ctx.params;
  return proxyRequest(req, path);
}

export async function DELETE(req: NextRequest, ctx: RouteContext) {
  const { path } = await ctx.params;
  return proxyRequest(req, path);
}
