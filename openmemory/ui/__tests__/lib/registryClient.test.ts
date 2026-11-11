jest.mock("@/lib/api-client", () => ({
  apiClient: {
    get: jest.fn(),
    post: jest.fn(),
  },
}));

import { apiClient } from "@/lib/api-client";
import {
  buildPublishManifest,
  buildRegistryResourcePath,
  dedupeLatestResources,
  getRegistryResource,
  listRegistryResources,
  publishRegistryManifest,
  registryDependencySummary,
  registryResourceDescription,
  registryResourceSearchText,
  registrySourceSummary,
  validatePublishDraft,
  fetchInstallRecipe,
  getStoreApp,
  REGISTRY_KIND_API_KIND,
  type RegistryResource,
} from "@/lib/registry-client";

const mockedApiClient = apiClient as jest.Mocked<typeof apiClient>;

describe("registry-client aplicativos", () => {
  beforeEach(() => {
    mockedApiClient.get.mockReset();
    mockedApiClient.post.mockReset();
  });

  it("mapeia localapps para o kind LocalApp", () => {
    expect(REGISTRY_KIND_API_KIND.localapps).toBe("LocalApp");
  });

  it("getStoreApp lê a latest pelo nome codificado", async () => {
    mockedApiClient.get.mockResolvedValueOnce({ data: { name: "horas app/x" } });

    const view = await getStoreApp("horas app/x");

    expect(mockedApiClient.get).toHaveBeenCalledWith("/api-proxy/api/v1/store/apps/horas%20app%2Fx");
    expect(view).toEqual({ name: "horas app/x" });
  });

  it("getStoreApp lê uma versão com nome e versão codificados", async () => {
    mockedApiClient.get.mockResolvedValueOnce({ data: {} });

    await getStoreApp("horas app/x", "1/2");

    expect(mockedApiClient.get).toHaveBeenCalledWith(
      "/api-proxy/api/v1/store/apps/horas%20app%2Fx/1%2F2",
    );
  });

  it("a receita de instalação de localapps pede o kind app", async () => {
    mockedApiClient.post.mockResolvedValueOnce({ data: { steps: [] } });

    await fetchInstallRecipe({ kind: "localapps", name: "horas-app", tag: "latest", target: "cursor" });

    expect(mockedApiClient.post).toHaveBeenCalledWith("/api-proxy/api/v1/store/install-recipes", {
      kind: "app",
      name: "horas-app",
      tag: "latest",
      target: "cursor",
    });
  });
});

describe("registry-client", () => {
  beforeEach(() => {
    mockedApiClient.get.mockReset();
    mockedApiClient.post.mockReset();
  });

  it("lista sem latestOnly (tag semver não é 'latest')", async () => {
    mockedApiClient.get.mockResolvedValueOnce({
      data: { items: [] },
    });

    await listRegistryResources("skills");

    expect(mockedApiClient.get).toHaveBeenCalledWith(
      "/registry-api/v0/skills",
      expect.objectContaining({
        params: expect.objectContaining({
          namespace: "all",
          limit: 100,
        }),
      }),
    );
    const params = mockedApiClient.get.mock.calls[0][1]?.params as Record<
      string,
      unknown
    >;
    expect(params.latestOnly).toBeUndefined();
    expect(buildRegistryResourcePath("skills", "team/skill", "v1")).toBe(
      "/registry-api/v0/skills/team%2Fskill/v1",
    );
  });

  it("dedupeLatestResources mantém a tag mais recente por nome", () => {
    const items = dedupeLatestResources([
      {
        registryKind: "skills",
        metadata: {
          name: "commit",
          tag: "1.0.0",
          updatedAt: "2026-01-01T00:00:00Z",
        },
      },
      {
        registryKind: "skills",
        metadata: {
          name: "commit",
          tag: "1.0.1",
          updatedAt: "2026-06-01T00:00:00Z",
        },
      },
    ]);
    expect(items).toHaveLength(1);
    expect(items[0].metadata.tag).toBe("1.0.1");
  });

  it("busca detalhe preservando namespace quando informado", async () => {
    mockedApiClient.get.mockResolvedValueOnce({
      data: {
        kind: "Skill",
        metadata: { namespace: "team", name: "skill", tag: "v1" },
        spec: {},
      },
    });

    const detail = await getRegistryResource("skills", "skill", "v1", "team");

    expect(mockedApiClient.get).toHaveBeenCalledWith(
      "/registry-api/v0/skills/skill/v1",
      { params: { namespace: "team" } },
    );
    expect(detail.registryKind).toBe("skills");
  });

  it("publica manifesto via /registry-api/v0/apply como YAML", async () => {
    mockedApiClient.post.mockResolvedValueOnce({
      data: { results: [{ kind: "Skill", name: "demo", status: "created" }] },
    });

    const response = await publishRegistryManifest("kind: Skill\n");

    expect(mockedApiClient.post).toHaveBeenCalledWith(
      "/registry-api/v0/apply",
      "kind: Skill\n",
      { headers: { "content-type": "application/yaml" } },
    );
    expect(response.results?.[0].status).toBe("created");
  });

  it("gera manifesto mínimo e valida campos obrigatórios", () => {
    expect(
      validatePublishDraft({
        kind: "skills",
        name: "",
        tag: "latest",
        title: "",
        description: "",
        sourceRepository: "",
        promptContent: "",
        skillContent: "",
      }),
    ).toEqual([
      "Informe o nome do recurso.",
      "Informe um título curto.",
      "Informe o conteúdo completo da Skill, incluindo SKILL.md.",
    ]);

    const inline = buildPublishManifest({
      kind: "skills",
      name: "demo",
      tag: "1.0.0",
      title: "Demo Skill",
      description: "Ajuda em tarefas repetitivas",
      sourceRepository: "",
      promptContent: "",
      skillContent: "---\nname: demo\n---\n# Demo",
    });
    expect(inline).toContain("kind: Skill");
    expect(inline).toContain("agentregistry.mem0.ai/skill-md:");
    expect(inline).not.toContain("source:");

    const manifest = buildPublishManifest({
      kind: "skills",
      name: "team/demo",
      tag: "v1",
      title: "Demo Skill",
      description: "Ajuda em tarefas repetitivas",
      sourceRepository: "https://github.com/acme/demo",
      promptContent: "",
      skillContent: "",
    });

    expect(manifest).toContain("kind: Skill");
    expect(manifest).toContain('name: "team/demo"');
    expect(manifest).toContain('tag: "v1"');
    expect(manifest).not.toContain("source:");
  });

  it("extrai texto de busca, origem e dependências úteis", () => {
    const resource: RegistryResource = {
      registryKind: "agents",
      kind: "Agent",
      metadata: {
        name: "agent",
        tag: "latest",
        labels: { team: "platform" },
      },
      spec: {
        title: "Agente Demo",
        description: "Executa fluxos internos",
        source: {
          repository: {
            url: "https://github.com/acme/agent",
            branch: "main",
          },
        },
        skills: [{ name: "skill-a", tag: "v1" }],
        mcpServers: [{ name: "mcp-a" }],
      },
    };

    expect(registryResourceDescription(resource)).toBe("Executa fluxos internos");
    expect(registryResourceSearchText(resource)).toContain("platform");
    expect(registrySourceSummary(resource)).toEqual([
      "Repositório: https://github.com/acme/agent (main)",
    ]);
    expect(registryDependencySummary(resource)).toEqual([
      "Skill: skill-a@v1",
      "MCP: mcp-a",
    ]);
  });

  it("resume skill inline sem exigir git", () => {
    expect(
      registrySourceSummary({
        registryKind: "skills",
        metadata: {
          name: "commit",
          annotations: {
            "agentregistry.mem0.ai/skill-md": "# Commit skill",
          },
        },
        spec: { title: "Commit" },
      }),
    ).toEqual(["Conteúdo embutido (SKILL.md)"]);
  });
});
