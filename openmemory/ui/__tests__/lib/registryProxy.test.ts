import {
  applyLegacyRegistryAuthorization,
  hasRegistryAuthorization,
  isRegistryProxyPathAllowed,
  registryProxyTarget,
} from "@/lib/registry-proxy";

describe("registry proxy allowlist", () => {
  it("permite leituras de catálogo nativo", () => {
    expect(isRegistryProxyPathAllowed("GET", ["v0", "skills"])).toBe(true);
    expect(isRegistryProxyPathAllowed("GET", ["v0", "mcpservers", "sysmo"])).toBe(
      true,
    );
    expect(
      isRegistryProxyPathAllowed("GET", ["v0", "agents", "team/agent", "latest"]),
    ).toBe(true);
    expect(
      isRegistryProxyPathAllowed("GET", ["v0", "prompts", "assist", "tags"]),
    ).toBe(true);
  });

  it("permite leitura de scripts e recusa gravação direta", () => {
    expect(isRegistryProxyPathAllowed("GET", ["v0", "scripts"])).toBe(true);
    expect(
      isRegistryProxyPathAllowed("GET", ["v0", "scripts", "gcenfe02-xml", "latest"]),
    ).toBe(true);
    expect(
      isRegistryProxyPathAllowed("DELETE", ["v0", "scripts", "gcenfe02-xml", "latest"]),
    ).toBe(false);
  });

  it("permite leitura compatível do MCP Registry v0.1", () => {
    expect(isRegistryProxyPathAllowed("GET", ["v0.1", "servers"])).toBe(true);
    expect(
      isRegistryProxyPathAllowed("GET", [
        "v0.1",
        "servers",
        "team/server",
        "versions",
      ]),
    ).toBe(true);
    expect(
      isRegistryProxyPathAllowed("GET", [
        "v0.1",
        "servers",
        "team/server",
        "versions",
        "latest",
      ]),
    ).toBe(true);
  });

  it("permite somente POST /v0/apply para publicação", () => {
    expect(isRegistryProxyPathAllowed("POST", ["v0", "apply"])).toBe(true);
    expect(isRegistryProxyPathAllowed("POST", ["v0", "skills"])).toBe(false);
    expect(isRegistryProxyPathAllowed("DELETE", ["v0", "apply"])).toBe(false);
    expect(
      isRegistryProxyPathAllowed("PUT", ["v0", "skills", "demo", "latest", "artifact"]),
    ).toBe(true);
    expect(
      isRegistryProxyPathAllowed("DELETE", ["v0", "skills", "demo", "latest"]),
    ).toBe(true);
  });

  it("nega endpoints fora do catálogo seguro", () => {
    expect(isRegistryProxyPathAllowed("GET", ["v0", "deployments"])).toBe(false);
    expect(isRegistryProxyPathAllowed("GET", ["v0", "runtimes"])).toBe(false);
    expect(isRegistryProxyPathAllowed("PUT", ["v0", "skills", "x"])).toBe(false);
    expect(isRegistryProxyPathAllowed("PATCH", ["v0", "skills", "x"])).toBe(false);
    expect(isRegistryProxyPathAllowed("DELETE", ["v0", "skills", "x"])).toBe(false);
  });
});

describe("registry proxy auth and target helpers", () => {
  it("aceita apenas Authorization Bearer preenchido", () => {
    expect(
      hasRegistryAuthorization(new Headers({ authorization: "Bearer jwt" })),
    ).toBe(true);
    expect(
      hasRegistryAuthorization(new Headers({ authorization: "Bearer local" })),
    ).toBe(true);
    expect(
      hasRegistryAuthorization(new Headers({ authorization: "Bearer omtk_abc" })),
    ).toBe(true);
    expect(hasRegistryAuthorization(new Headers())).toBe(false);
    expect(hasRegistryAuthorization(new Headers({ authorization: "Basic x" }))).toBe(
      false,
    );
    expect(hasRegistryAuthorization(new Headers({ authorization: "Bearer   " }))).toBe(
      false,
    );
  });

  it("monta URL upstream preservando segmentos com barra codificada", () => {
    expect(
      registryProxyTarget(
        "http://agentregistry:8080",
        ["v0", "skills", "team/skill", "latest"],
        "?namespace=all",
      ),
    ).toBe("http://agentregistry:8080/v0/skills/team%2Fskill/latest?namespace=all");
  });

  it("em UI legado injeta Bearer local quando Authorization está ausente", () => {
    const prevAuth = process.env.AUTH_UI_REQUIRED;
    const prevGoogle = process.env.GOOGLE_CLIENT_ID;
    const prevPublicGoogle = process.env.NEXT_PUBLIC_GOOGLE_CLIENT_ID;
    process.env.AUTH_UI_REQUIRED = "0";
    delete process.env.GOOGLE_CLIENT_ID;
    delete process.env.NEXT_PUBLIC_GOOGLE_CLIENT_ID;

    const out = applyLegacyRegistryAuthorization(new Headers());
    expect(out.get("authorization")).toBe("Bearer local");

    process.env.AUTH_UI_REQUIRED = prevAuth;
    process.env.GOOGLE_CLIENT_ID = prevGoogle;
    process.env.NEXT_PUBLIC_GOOGLE_CLIENT_ID = prevPublicGoogle;
  });

  it("com Google auth exigido não injeta legado e preserva JWT existente", () => {
    const prevAuth = process.env.AUTH_UI_REQUIRED;
    process.env.AUTH_UI_REQUIRED = "1";

    expect(
      applyLegacyRegistryAuthorization(new Headers()).has("authorization"),
    ).toBe(false);

    const withJwt = applyLegacyRegistryAuthorization(
      new Headers({ authorization: "Bearer jwt-session" }),
    );
    expect(withJwt.get("authorization")).toBe("Bearer jwt-session");

    process.env.AUTH_UI_REQUIRED = prevAuth;
  });
});
