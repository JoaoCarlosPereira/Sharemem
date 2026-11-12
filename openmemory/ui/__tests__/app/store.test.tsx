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
    expect(screen.getByLabelText("Versão do script")).toHaveValue("1");
    expect(mockedApiClient.get).toHaveBeenCalledWith(
      "/api-proxy/api/v1/store/scripts/gcenfe02-xml/1",
    );
  });
});

function appResource(name: string, version: string): RegistryResource {
  return {
    registryKind: "localapps",
    kind: "LocalApp",
    metadata: {
      namespace: "default",
      name,
      tag: "latest",
      updatedAt: "2026-11-10T10:00:00Z",
      annotations: { "sharemem.io/versao": version, "sharemem.io/autor": "pessoa-1" },
    },
    spec: {
      description: "Lança as horas da planilha no Redmine",
      effects: ["rede", "escrita-local"],
      area: "financeiro",
    },
  };
}

function appView(version: string, isLatest: boolean, overrides: Record<string, unknown> = {}) {
  return {
    name: "horas-app",
    version,
    is_latest: isLatest,
    sha256: `sha-app-${version}`,
    wheel_name: `horas_app-${version}-py3-none-any.whl`,
    python_requires: ">=3.10",
    card: {
      title: null,
      description: "Lança as horas da planilha no Redmine",
      usage: "horas-app lancar",
      requirements: ["python>=3.10"],
      effects: ["rede", "escrita-local"],
      area: "financeiro",
      tags: ["redmine"],
      author: "pessoa-1",
      published_at: "2026-11-10T00:00:00+00:00",
    },
    commands: { start: "horas-app lancar", verify: "horas-app verificar" },
    settings: [
      {
        name: "REDMINE_URL",
        description: "Endereço do Redmine",
        required: true,
        secret: false,
        env: "REDMINE_URL",
        file: ".claude/apis/redmine.env",
        key: "REDMINE_URL",
        value: "https://redmine.exemplo.com.br",
      },
      {
        name: "REDMINE_API_KEY",
        description: "Chave de API do Redmine",
        required: true,
        secret: true,
        env: "REDMINE_API_KEY",
        value: "chave-que-nunca-aparece",
      },
      {
        name: "planilha",
        description: "Planilha de horas",
        required: false,
        secret: false,
        file: "config.ini",
        key: "planilha",
      },
    ],
    versions: [
      { version: "3", author: "pessoa-1", published_at: null, is_latest: true },
      { version: "1", author: "pessoa-2", published_at: null, is_latest: false },
    ],
    ...overrides,
  };
}

const APP_VIEWS_BASE: Record<string, unknown> = {
  "horas-app": appView("3", true),
  "horas-app/1": appView("1", false),
};

function mockAppCatalog(
  appNames: string[] = ["horas-app"],
  views: Record<string, unknown> = APP_VIEWS_BASE,
) {
  const prefix = "/api-proxy/api/v1/store/apps/";
  mockedApiClient.get.mockImplementation((url) => {
    const path = String(url);
    if (path === "/registry-api/v0/skills") {
      return Promise.resolve({ data: { items: [skill] } });
    }
    if (path === "/registry-api/v0/scripts") {
      return Promise.resolve({ data: { items: [scriptResource("latest", "3", "2026-10-27T10:00:00Z")] } });
    }
    if (path === "/registry-api/v0/localapps") {
      return Promise.resolve({ data: { items: appNames.map((name) => appResource(name, "3")) } });
    }
    if (path.startsWith(prefix)) {
      const view = views[path.slice(prefix.length)];
      return view ? Promise.resolve({ data: view }) : Promise.reject({});
    }
    if (path === "/api-proxy/api/v1/store/scripts/gcenfe02-xml") {
      return Promise.resolve({ data: scriptView("3", true) });
    }
    if (path.includes("/registry-api/v0/skills/team%2Fdemo-skill/latest")) {
      return Promise.resolve({ data: skill });
    }
    return Promise.resolve({ data: { items: [] } });
  });
}

describe("StorePage aplicativos", () => {
  beforeEach(() => {
    mockedApiClient.get.mockReset();
    mockAppCatalog();
  });

  async function openAppByName(name: string) {
    render(<StorePage />);
    await screen.findByText("Demo Skill");
    fireEvent.click(within(screen.getByLabelText("Filtros por tipo")).getByRole("button", { name: /^Aplicativos/ }));
    fireEvent.click(screen.getAllByText(name, { selector: "p" })[0].closest("button") as HTMLButtonElement);
    return screen.findByRole("region", { name: "Detalhe do aplicativo" });
  }

  const openApp = () => openAppByName("horas-app");

  it("filtro Aplicativos lista só itens localapps com a ficha resumida", async () => {
    render(<StorePage />);
    await screen.findByText("Demo Skill");

    const filter = within(screen.getByLabelText("Filtros por tipo")).getByRole("button", { name: /^Aplicativos/ });
    expect(filter).toHaveTextContent("1");
    fireEvent.click(filter);

    expect(screen.queryByText("Demo Skill")).not.toBeInTheDocument();
    const cards = screen.getAllByText("horas-app", { selector: "p" });
    expect(cards).toHaveLength(1);
    const card = cards[0].closest("button") as HTMLButtonElement;
    expect(card).toHaveTextContent("Aplicativos");
    expect(card).toHaveTextContent("Lança as horas da planilha no Redmine");
    expect(card).toHaveTextContent("rede, escrita-local");
    expect(card).toHaveTextContent("pessoa-1");
    expect(card).toHaveTextContent("versão 3");
    expect(card.querySelector("svg.lucide-package-plus")).not.toBeNull();
    expect(mockedApiClient.get).toHaveBeenCalledWith("/registry-api/v0/localapps", {
      params: { namespace: "all", limit: 100, latestOnly: true },
    });
  });

  it("Aplicativos não é tipo publicável", async () => {
    render(<StorePage />);
    await screen.findByText("Demo Skill");

    expect(screen.queryByRole("option", { name: "Aplicativos" })).not.toBeInTheDocument();
  });

  it("abrir horas-app mostra o que precisa configurar, sem valor e com o segredo marcado", async () => {
    const region = await openApp();

    expect(within(region).getByText("O que precisa configurar")).toBeInTheDocument();
    const url = within(region).getByText("REDMINE_URL", { selector: "span" }).closest("li") as HTMLElement;
    expect(url).toHaveTextContent("Endereço do Redmine");
    expect(url).toHaveTextContent("Obrigatório: sim");
    expect(url).toHaveTextContent("Segredo: não");
    expect(url).toHaveTextContent(
      "Onde fica: variável de ambiente REDMINE_URL ou arquivo .claude/apis/redmine.env (chave REDMINE_URL)",
    );
    const key = within(region).getByText("REDMINE_API_KEY", { selector: "span" }).closest("li") as HTMLElement;
    expect(key).toHaveTextContent("Chave de API do Redmine");
    expect(key).toHaveTextContent("Obrigatório: sim");
    expect(key).toHaveTextContent("Segredo: sim");
    const file = within(region).getByText("planilha", { selector: "span" }).closest("li") as HTMLElement;
    expect(file).toHaveTextContent("Obrigatório: não");
    expect(file).toHaveTextContent("Onde fica: arquivo config.ini (chave planilha)");
    expect(region).not.toHaveTextContent("chave-que-nunca-aparece");
    expect(region).not.toHaveTextContent("https://redmine.exemplo.com.br");
  });

  it("mostra a ficha completa dentro do detalhe", async () => {
    const region = await openApp();

    expect(within(region).getByRole("heading", { level: 2, name: "horas-app" })).toBeInTheDocument();
    expect(within(region).getByText("Lança as horas da planilha no Redmine")).toBeInTheDocument();
    expect(within(region).getByText("horas_app-3-py3-none-any.whl")).toBeInTheDocument();
    expect(within(region).getByText("Efeitos: rede, escrita-local")).toBeInTheDocument();
    expect(within(region).getByText("Projeto/área: financeiro")).toBeInTheDocument();
    expect(within(region).getByText("Python: >=3.10")).toBeInTheDocument();
    expect(within(region).getByText("Requisitos: python>=3.10")).toBeInTheDocument();
    expect(within(region).getByText("Tags: redmine")).toBeInTheDocument();
    expect(within(region).getByText("Iniciar: horas-app lancar")).toBeInTheDocument();
    expect(within(region).getByText("Verificar: horas-app verificar")).toBeInTheDocument();
    expect(within(region).getByText("Autor: pessoa-1")).toBeInTheDocument();
    expect(within(region).getByText("Publicado em: 2026-11-10T00:00:00+00:00")).toBeInTheDocument();
    expect(within(region).getByText("sha256: sha-app-3")).toBeInTheDocument();
  });

  it("omite requisitos e tags vazios e usa traço, título e versão sem autor", async () => {
    mockAppCatalog(["horas-app"], {
      "horas-app": appView("2", true, {
        card: {
          title: "Horas da equipe",
          description: "Lança horas",
          usage: "",
          requirements: [],
          effects: [],
          area: "financeiro",
          tags: [],
          author: null,
          published_at: null,
        },
        settings: [],
        versions: [{ version: "2", author: null, published_at: null, is_latest: true }],
      }),
    });
    const region = await openApp();

    expect(within(region).getByRole("heading", { level: 2, name: "Horas da equipe" })).toBeInTheDocument();
    expect(within(region).queryByText(/Requisitos:/)).not.toBeInTheDocument();
    expect(within(region).queryByText(/Tags:/)).not.toBeInTheDocument();
    expect(within(region).getByText("Autor: -")).toBeInTheDocument();
    expect(within(region).getByText("Publicado em: -")).toBeInTheDocument();
    expect(within(region).getByRole("option", { name: "2 (latest)" })).toBeInTheDocument();
    expect(within(region).getByText("O que precisa configurar")).toBeInTheDocument();
    expect(within(region).getByText("Nenhuma configuração necessária.")).toBeInTheDocument();
  });

  it("mostra onde fica a configuração por arquivo sem chave e sem origem", async () => {
    mockAppCatalog(["horas-app"], {
      "horas-app": appView("3", true, {
        settings: [
          { name: "SO_ARQUIVO", description: "Só arquivo", required: false, secret: false, file: "config.ini" },
          { name: "SEM_ORIGEM", description: "Sem origem", required: false, secret: false },
        ],
      }),
    });
    const region = await openApp();

    const file = within(region).getByText("SO_ARQUIVO", { selector: "span" }).closest("li") as HTMLElement;
    expect(file).toHaveTextContent("Onde fica: arquivo config.ini");
    expect(file).not.toHaveTextContent("chave");
    const none = within(region).getByText("SEM_ORIGEM", { selector: "span" }).closest("li") as HTMLElement;
    expect(none).toHaveTextContent("Onde fica: -");
    expect(within(region).queryByText("Nenhuma configuração necessária.")).not.toBeInTheDocument();
  });

  it("mostra as versões com a latest marcada e troca de versão", async () => {
    const region = await openApp();

    const select = within(region).getByLabelText("Versão do aplicativo") as HTMLSelectElement;
    expect(select).toHaveValue("3");
    expect(within(select).getByRole("option", { name: "3 (latest) · pessoa-1" })).toBeInTheDocument();
    expect(within(select).getByRole("option", { name: "1 · pessoa-2" })).toBeInTheDocument();

    fireEvent.change(select, { target: { value: "1" } });
    await waitFor(() => {
      expect(within(region).getByText("sha256: sha-app-1")).toBeInTheDocument();
    });
    expect(within(region).getByLabelText("Versão do aplicativo")).toHaveValue("1");
    expect(mockedApiClient.get).toHaveBeenCalledWith("/api-proxy/api/v1/store/apps/horas-app/1");
  });

  it("mostra como instalar pelo agente e não tem botão de publicar", async () => {
    const region = await openApp();

    expect(within(region).getByText("Como instalar")).toBeInTheDocument();
    expect(
      within(region).getByText("peça ao agente para instalar o app horas-app"),
    ).toBeInTheDocument();
    expect(within(region).queryByRole("button", { name: /publicar/i })).not.toBeInTheDocument();
  });

  it("falha ao carregar o aplicativo mostra o alerta", async () => {
    mockAppCatalog(["horas-app"], {});
    render(<StorePage />);
    await screen.findByText("Demo Skill");
    fireEvent.click(within(screen.getByLabelText("Filtros por tipo")).getByRole("button", { name: /^Aplicativos/ }));
    fireEvent.click(screen.getAllByText("horas-app", { selector: "p" })[0].closest("button") as HTMLButtonElement);

    expect(await screen.findByRole("alert")).toHaveTextContent("Falha ao carregar o aplicativo.");
    expect(screen.queryByRole("region", { name: "Detalhe do aplicativo" })).not.toBeInTheDocument();
  });

  it("alterna entre aplicativo, script, skill e outro aplicativo mostrando só o painel certo", async () => {
    mockAppCatalog(["horas-app", "contas-app"], {
      ...APP_VIEWS_BASE,
      "contas-app": appView("2", true, {
        name: "contas-app",
        versions: [{ version: "2", author: "pessoa-3", published_at: null, is_latest: true }],
        card: { ...appView("2", true).card, title: "Contas a pagar" },
      }),
    });
    render(<StorePage />);
    await screen.findByText("Demo Skill");

    const cardOf = (name: string) =>
      screen.getAllByText(name, { selector: "p" })[0].closest("button") as HTMLButtonElement;
    const appPanel = () => screen.queryByRole("region", { name: "Detalhe do aplicativo" });
    const scriptPanel = () => screen.queryByLabelText("Código do script");
    const skillPanel = () => screen.queryByText(/Repositório: https:\/\/github.com\/acme\/demo-skill/);
    const selectedCards = () =>
      screen
        .getAllByRole("button")
        .filter((button) => button.className.includes("border-blue-500/60") && button.className.includes("ring-1"));

    fireEvent.click(cardOf("horas-app"));
    expect(await screen.findByRole("region", { name: "Detalhe do aplicativo" })).toBeInTheDocument();
    expect(scriptPanel()).not.toBeInTheDocument();
    expect(skillPanel()).not.toBeInTheDocument();
    expect(selectedCards()).toEqual([cardOf("horas-app")]);

    fireEvent.click(cardOf("gcenfe02-xml"));
    expect(await screen.findByLabelText("Código do script")).toBeInTheDocument();
    expect(appPanel()).not.toBeInTheDocument();
    expect(skillPanel()).not.toBeInTheDocument();
    expect(selectedCards()).toEqual([cardOf("gcenfe02-xml")]);

    fireEvent.click(cardOf("horas-app"));
    expect(await screen.findByRole("region", { name: "Detalhe do aplicativo" })).toBeInTheDocument();
    expect(scriptPanel()).not.toBeInTheDocument();
    expect(skillPanel()).not.toBeInTheDocument();
    expect(selectedCards()).toEqual([cardOf("horas-app")]);

    fireEvent.click(cardOf("Demo Skill"));
    expect(await screen.findByText(/Repositório: https:\/\/github.com\/acme\/demo-skill/)).toBeInTheDocument();
    expect(appPanel()).not.toBeInTheDocument();
    expect(scriptPanel()).not.toBeInTheDocument();
    expect(selectedCards()).toEqual([cardOf("Demo Skill")]);

    fireEvent.click(cardOf("horas-app"));
    expect(await screen.findByRole("region", { name: "Detalhe do aplicativo" })).toBeInTheDocument();
    expect(scriptPanel()).not.toBeInTheDocument();
    expect(skillPanel()).not.toBeInTheDocument();
    expect(selectedCards()).toEqual([cardOf("horas-app")]);

    fireEvent.click(cardOf("contas-app"));
    expect(appPanel()).not.toBeInTheDocument();
    const second = await screen.findByRole("region", { name: "Detalhe do aplicativo" });
    expect(within(second).getByRole("heading", { level: 2, name: "Contas a pagar" })).toBeInTheDocument();
    expect(within(second).getByLabelText("Versão do aplicativo")).toHaveValue("2");
    expect(mockedApiClient.get).toHaveBeenCalledWith("/api-proxy/api/v1/store/apps/contas-app");
    expect(selectedCards()).toEqual([cardOf("contas-app")]);
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
