import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

jest.mock("@/lib/api-client", () => ({
  apiClient: {
    get: jest.fn(),
    post: jest.fn(),
    put: jest.fn(),
    delete: jest.fn(),
  },
}));

jest.mock("@/hooks/useApiSessionReady", () => ({
  useApiSessionReady: () => true,
}));

import { apiClient } from "@/lib/api-client";
import StorePage from "@/app/store/page";
import type { RegistryResource } from "@/lib/registry-client";

const mockedApiClient = apiClient as jest.Mocked<typeof apiClient>;

const skill: RegistryResource = {
  registryKind: "skills",
  apiVersion: "agentregistry.dev/v1alpha1",
  kind: "Skill",
  metadata: {
    namespace: "default",
    name: "team/demo-skill",
    tag: "latest",
    labels: { team: "platform" },
  },
  spec: {
    title: "Demo Skill",
    description: "Automatiza tarefas repetitivas",
    source: {
      repository: {
        url: "https://github.com/acme/demo-skill",
      },
    },
  },
};

function scriptResource(tag: string, version: string, updatedAt: string): RegistryResource {
  return {
    registryKind: "scripts",
    kind: "Script",
    metadata: {
      namespace: "default",
      name: "gcenfe02-xml",
      tag,
      updatedAt,
      annotations: { "sharemem.io/versao": version, "sharemem.io/autor": "pessoa-1" },
    },
    spec: {
      description: "Decodifica a coluna GCENFE02.XML",
      usage: "python gcenfe02_xml.py <arquivo>",
      fileName: "gcenfe02_xml.py",
      language: "python",
      effects: ["somente-leitura"],
      area: "sysmo-s1",
      content: `print(${version})\n`,
    },
  };
}

function scriptView(version: string, isLatest: boolean) {
  return {
    name: "gcenfe02-xml",
    version,
    is_latest: isLatest,
    file_name: "gcenfe02_xml.py",
    content: `print('v${version}')\n`,
    sha256: `sha-${version}`,
    warning: null,
    card: {
      title: null,
      description: "Decodifica a coluna GCENFE02.XML",
      usage: "python gcenfe02_xml.py <arquivo>",
      language: "python",
      requirements: ["python>=3.8"],
      effects: ["somente-leitura"],
      area: "sysmo-s1",
      tags: ["nfe"],
      author: "pessoa-1",
      published_at: "2026-10-27T00:00:00+00:00",
      downloads_30d: 4,
    },
    versions: [
      { version: "3", author: "pessoa-1", published_at: null, is_latest: true },
      { version: "1", author: "pessoa-2", published_at: null, is_latest: false },
    ],
  };
}

function mockScriptCatalog() {
  mockedApiClient.get.mockImplementation((url, config) => {
    const path = String(url);
    if (path === "/registry-api/v0/skills") {
      return Promise.resolve({ data: { items: [skill] } });
    }
    if (path === "/registry-api/v0/scripts") {
      const latest = scriptResource("latest", "3", "2026-10-27T10:00:00Z");
      const older = scriptResource("1", "1", "2026-10-27T11:00:00Z");
      const latestOnly = (config as { params?: Record<string, unknown> })?.params?.latestOnly;
      return Promise.resolve({ data: { items: latestOnly ? [latest] : [latest, older] } });
    }
    if (path === "/api-proxy/api/v1/store/scripts/gcenfe02-xml") {
      return Promise.resolve({ data: scriptView("3", true) });
    }
    if (path === "/api-proxy/api/v1/store/scripts/gcenfe02-xml/1") {
      return Promise.resolve({ data: scriptView("1", false) });
    }
    return Promise.resolve({ data: { items: [] } });
  });
}

describe("StorePage scripts", () => {
  beforeEach(() => {
    mockedApiClient.get.mockReset();
    mockScriptCatalog();
  });

  it("filtro Scripts lista só scripts da latest com a ficha resumida", async () => {
    render(<StorePage />);
    await screen.findByText("Demo Skill");

    fireEvent.click(within(screen.getByLabelText("Filtros por tipo")).getByRole("button", { name: /^Scripts/ }));

    expect(screen.queryByText("Demo Skill")).not.toBeInTheDocument();
    const cards = screen.getAllByText("gcenfe02-xml", { selector: "p" });
    expect(cards).toHaveLength(1);
    const card = cards[0].closest("button") as HTMLButtonElement;
    expect(card).toHaveTextContent("Decodifica a coluna GCENFE02.XML");
    expect(card).toHaveTextContent("python");
    expect(card).toHaveTextContent("somente-leitura");
    expect(card).toHaveTextContent("pessoa-1");
    expect(card).toHaveTextContent("versão 3");
  });

  it("abrir um script mostra ficha, código da versão escolhida e copia o exemplo", async () => {
    const writeText = jest.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    render(<StorePage />);
    await screen.findByText("Demo Skill");
    fireEvent.click(within(screen.getByLabelText("Filtros por tipo")).getByRole("button", { name: /^Scripts/ }));

    fireEvent.click(screen.getAllByText("gcenfe02-xml", { selector: "p" })[0].closest("button") as HTMLButtonElement);

    const code = await screen.findByLabelText("Código do script");
    expect(code).toHaveTextContent("print('v3')");
    expect(screen.getByText("python gcenfe02_xml.py <arquivo>")).toBeInTheDocument();
    expect(screen.getByText(/sysmo-s1/)).toBeInTheDocument();
    expect(screen.getByText(/python>=3.8/)).toBeInTheDocument();
    expect(screen.getByText(/Downloads \(30 dias\): 4/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Copiar exemplo de uso/i }));
    expect(writeText).toHaveBeenCalledWith("python gcenfe02_xml.py <arquivo>");

    fireEvent.change(screen.getByLabelText("Versão do script"), { target: { value: "1" } });
    await waitFor(() => {
      expect(screen.getByLabelText("Código do script")).toHaveTextContent("print('v1')");
    });
    expect(mockedApiClient.get).toHaveBeenCalledWith(
      "/api-proxy/api/v1/store/scripts/gcenfe02-xml/1",
    );
  });
});

describe("StorePage", () => {
  beforeEach(() => {
    mockedApiClient.get.mockReset();
    mockedApiClient.post.mockReset();
    mockedApiClient.put.mockReset();
    mockedApiClient.delete.mockReset();
    mockedApiClient.get.mockImplementation((url) => {
      if (String(url) === "/registry-api/v0/skills") {
        return Promise.resolve({ data: { items: [skill] } });
      }
      if (String(url).includes("/registry-api/v0/skills/team%2Fdemo-skill/latest")) {
        return Promise.resolve({ data: skill });
      }
      return Promise.resolve({ data: { items: [] } });
    });
    mockedApiClient.post.mockResolvedValue({
      data: {
        results: [{ kind: "Skill", name: "team/new-skill", tag: "latest", status: "created" }],
      },
    });
    mockedApiClient.put.mockResolvedValue({ data: {} });
  });

  it("renderiza listagem autenticada e carrega detalhe", async () => {
    render(<StorePage />);

    expect(await screen.findByText("Demo Skill")).toBeInTheDocument();
    expect(screen.getByText("Automatiza tarefas repetitivas")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Buscar na Store"), {
      target: { value: "platform" },
    });
    expect(screen.getByText("Demo Skill")).toBeInTheDocument();

    fireEvent.click(screen.getByText("Demo Skill").closest("button") as HTMLButtonElement);

    await waitFor(() => {
      expect(mockedApiClient.get).toHaveBeenCalledWith(
        "/registry-api/v0/skills/team%2Fdemo-skill/latest",
        { params: { namespace: "default" } },
      );
    });
    expect(await screen.findByText(/Repositório: https:\/\/github.com\/acme\/demo-skill/)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Gerar receita de instalação/i }),
    ).toBeEnabled();

    mockedApiClient.post.mockResolvedValueOnce({
      data: {
        version: "1",
        resource_kind: "skill",
        name: "team/demo-skill",
        tag: "latest",
        target: "cursor",
        steps: [{ id: "copy-resource", type: "copy", to: ".cursor/skills/team/demo-skill" }],
      },
    });
    fireEvent.click(screen.getByRole("button", { name: /Gerar receita de instalação/i }));
    await waitFor(() => {
      expect(mockedApiClient.post).toHaveBeenCalledWith(
        "/api-proxy/api/v1/store/install-recipes",
        {
          kind: "skill",
          name: "team/demo-skill",
          tag: "latest",
          target: "cursor",
        },
      );
    });
    expect(await screen.findByText(/Receita cursor/i)).toBeInTheDocument();
  });

  it("publica uma Skill completa via API da Store", async () => {
    render(<StorePage />);
    await screen.findByText("Demo Skill");

    fireEvent.change(screen.getByLabelText("Nome do recurso"), {
      target: { value: "team/new-skill" },
    });
    fireEvent.change(screen.getByLabelText("Título"), {
      target: { value: "New Skill" },
    });
    fireEvent.change(screen.getByLabelText("Conteúdo da skill"), {
      target: { value: "# New Skill\n\nInstruções em português para a equipe." },
    });

    fireEvent.click(screen.getByRole("button", { name: /Publicar via \/v0\/apply/i }));

    await waitFor(() => {
      expect(mockedApiClient.put).toHaveBeenCalledWith(
        "/api-proxy/api/v1/store/skills/team%2Fnew-skill/latest",
        expect.objectContaining({
          name: "team/new-skill",
          tag: "latest",
          language: "pt-BR",
          files: [expect.objectContaining({ path: "SKILL.md", encoding: "utf-8" })],
        }),
      );
    });
    expect(await screen.findByText(/Skill completa publicada com sucesso/)).toBeInTheDocument();
  });
});
