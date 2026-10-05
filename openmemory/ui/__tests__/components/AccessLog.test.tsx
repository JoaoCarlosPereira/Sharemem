import React from "react";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Provider } from "react-redux";
import { configureStore } from "@reduxjs/toolkit";
import memoriesReducer, {
  setAccessLogMeta,
  setAccessLogs,
} from "@/store/memoriesSlice";
import type { AccessLogEntry, AccessLogMeta } from "@/types/accessLog";
import {
  accessChannelLabel,
  accessTypeLabel,
  isAnonymousWebReader,
  resolveAccessChannel,
  toAccessLogMeta,
  truncateQuery,
} from "@/lib/access-log";

jest.mock("next/image", () => ({
  __esModule: true,
  default: ({ unoptimized: _u, ...props }: Record<string, unknown>) => (
    // eslint-disable-next-line @next/next/no-img-element, jsx-a11y/alt-text
    <img {...(props as object)} />
  ),
}));

const fetchAccessLogs = jest.fn();
jest.mock("@/hooks/useMemoriesApi", () => ({
  useMemoriesApi: () => ({ fetchAccessLogs }),
}));

import { AccessLog } from "@/app/memory/[id]/components/AccessLog";

const ENTRIES: AccessLogEntry[] = [
  {
    id: "g1",
    app_name: "openmemory",
    display_name: "Ana Souza",
    avatar_url: "https://example.com/ana.png",
    client_name: "openmemory",
    // Leitura com sessão Google: ``ui:<User.id>`` (UUID do JWT).
    hostname: "ui:0f8c2b8e-3a4d-4c1e-9b7a-2d5e6f708192",
    source: "api",
    access_type: "get",
    channel: "web",
    channel_label: "Interface Web",
    count: 6,
    accessed_at: "2026-10-02T18:14:43+00:00",
    first_accessed_at: "2026-10-02T18:10:00+00:00",
    last_accessed_at: "2026-10-02T18:14:43+00:00",
  },
  {
    id: "g2",
    app_name: "claude-code",
    display_name: "Bruno Lima",
    avatar_url: "https://example.com/bruno.png",
    client_name: "claude-code",
    hostname: "S0258",
    source: "mcp",
    access_type: "search",
    channel: "mcp",
    count: 1,
    query: "regra de frete do Ecv212 com desconto e despesas acessórias na nota fiscal eletrônica",
    accessed_at: "2026-10-01T17:00:50+00:00",
  },
  {
    id: "g3",
    app_name: "cursor",
    client_name: "cursor",
    hostname: "S0176",
    display_name: "S0176",
    source: "mcp",
    access_type: "list",
    channel: "mcp",
    accessed_at: "2026-09-28T12:15:41+00:00",
  },
  {
    id: "g4",
    app_name: "openmemory",
    client_name: null,
    hostname: "S0302",
    display_name: "S0302",
    source: "compat_v3",
    access_type: "search",
    channel: "api",
    accessed_at: "2026-09-27T12:56:32+00:00",
  },
];

const META: AccessLogMeta = {
  total: 4,
  rawTotal: 9,
  grouped: true,
  groupWindowSeconds: 300,
  groupingTruncated: false,
  channel: null,
  channelCounts: { web: 6, mcp: 2, api: 1, other: 0 },
};

function renderWithStore(entries = ENTRIES, meta: AccessLogMeta | null = META) {
  const store = configureStore({ reducer: { memories: memoriesReducer } });
  fetchAccessLogs.mockImplementation(async () => {
    store.dispatch(setAccessLogs(entries));
    store.dispatch(setAccessLogMeta(meta));
  });
  render(
    <Provider store={store}>
      <AccessLog memoryId="mem-1" />
    </Provider>,
  );
  return store;
}

beforeEach(() => {
  fetchAccessLogs.mockReset();
});

describe("AccessLog (detalhe da memória)", () => {
  it("mostra pessoa, canal, tipo, contagem agrupada e query", async () => {
    renderWithStore();
    const items = await screen.findAllByTestId("access-log-entry");
    expect(items).toHaveLength(4);

    const web = within(items[0]);
    expect(web.getByText("Ana Souza")).toBeInTheDocument();
    expect(web.getByText("6×")).toBeInTheDocument();
    expect(web.getByText("Interface Web")).toBeInTheDocument();
    expect(web.getByText("Leitura")).toBeInTheDocument();
    expect(web.getByText(/15:10–15:14/)).toBeInTheDocument();
    expect(items[0].querySelector('img[src="https://example.com/ana.png"]')).not.toBeNull();

    const mcp = within(items[1]);
    expect(mcp.getByText("Bruno Lima")).toBeInTheDocument();
    expect(mcp.getByText("MCP · Claude Code")).toBeInTheDocument();
    expect(mcp.getByText("Busca")).toBeInTheDocument();
    expect(mcp.getByText(/“regra de frete do Ecv212/)).toBeInTheDocument();
    expect(mcp.queryByText(/×$/)).not.toBeInTheDocument();

    expect(within(items[2]).getByText("MCP · Cursor")).toBeInTheDocument();
    expect(within(items[2]).getByText("Listagem")).toBeInTheDocument();
    expect(within(items[3]).getByText("API")).toBeInTheDocument();

    // Ícones distintos por canal/tipo (data attrs no badge)
    expect(items[0].querySelector('[data-channel="web"]')).not.toBeNull();
    expect(items[1].querySelector('[data-channel="mcp"]')).not.toBeNull();
    expect(items[1].querySelector('[data-access-type="search"]')).not.toBeNull();

    expect(screen.getByText("9 acessos · 4 grupos")).toBeInTheDocument();
  });

  it("filtro 'Agentes' pede só leituras de MCP/API e volta à página 1", async () => {
    renderWithStore();
    await screen.findAllByTestId("access-log-entry");
    expect(fetchAccessLogs).toHaveBeenLastCalledWith("mem-1", 1, 10, { channel: null });

    const agents = screen.getByRole("button", { name: /Agentes/ });
    expect(agents).toHaveTextContent("3");
    await userEvent.click(agents);
    await waitFor(() =>
      expect(fetchAccessLogs).toHaveBeenLastCalledWith("mem-1", 1, 10, { channel: "agents" }),
    );
    expect(agents).toHaveAttribute("aria-pressed", "true");
  });

  it("paginação navega sobre os grupos", async () => {
    renderWithStore(ENTRIES, { ...META, total: 25 });
    await screen.findAllByTestId("access-log-entry");
    expect(screen.getByText("1 / 3")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Mais antigos" }));
    await waitFor(() =>
      expect(fetchAccessLogs).toHaveBeenLastCalledWith("mem-1", 2, 10, { channel: null }),
    );
  });

  it("leitura sem login (ID fixo do build) não vira pessoa nem foto", async () => {
    renderWithStore([
      {
        // Linha histórica: o build gravava NEXT_PUBLIC_USER_ID para todo navegador.
        id: "h1",
        app_name: "openmemory",
        display_name: "Ana Souza",
        avatar_url: "https://example.com/ana.png",
        hostname: "ui:S0293",
        source: "api",
        access_type: "get",
        channel: "web",
        accessed_at: "2026-10-02T18:14:43+00:00",
      },
      {
        id: "h2",
        app_name: "openmemory",
        display_name: "Interface Web (sem login)",
        hostname: "ui:anonymous",
        anonymous: true,
        source: "api",
        access_type: "list",
        channel: "web",
        accessed_at: "2026-10-02T18:10:00+00:00",
      },
    ]);
    const items = await screen.findAllByTestId("access-log-entry");
    expect(items).toHaveLength(2);
    for (const item of items) {
      expect(within(item).getByText("Interface Web (sem login)")).toBeInTheDocument();
      expect(item.querySelector('img[src="https://example.com/ana.png"]')).toBeNull();
    }
    expect(screen.queryByText("Ana Souza")).not.toBeInTheDocument();
  });

  it("aviso de histórico parcial quando o agrupamento foi truncado", async () => {
    renderWithStore(ENTRIES, { ...META, groupingTruncated: true });
    await screen.findAllByTestId("access-log-entry");
    expect(screen.getByTestId("access-log-partial")).toHaveTextContent(/Histórico parcial/);
  });

  it("sem truncamento não mostra aviso", async () => {
    renderWithStore();
    await screen.findAllByTestId("access-log-entry");
    expect(screen.queryByTestId("access-log-partial")).not.toBeInTheDocument();
  });

  it("vazio exibe mensagem", async () => {
    renderWithStore([], { ...META, total: 0, rawTotal: 0 });
    expect(await screen.findByText("Nenhum log de acesso disponível")).toBeInTheDocument();
  });
});

describe("lib/access-log", () => {
  it("rótulos de tipo", () => {
    expect(accessTypeLabel("get")).toBe("Leitura");
    expect(accessTypeLabel("search")).toBe("Busca");
    expect(accessTypeLabel("list")).toBe("Listagem");
    expect(accessTypeLabel(undefined)).toBe("Acesso");
  });

  it("canal com fallback para respostas antigas (sem channel)", () => {
    expect(resolveAccessChannel({ id: "1", app_name: "x", accessed_at: "", source: "mcp" })).toBe("mcp");
    expect(resolveAccessChannel({ id: "1", app_name: "x", accessed_at: "", hostname: "ui:S0293" })).toBe("web");
    expect(resolveAccessChannel({ id: "1", app_name: "x", accessed_at: "", source: "compat_v3" })).toBe("api");
    expect(
      accessChannelLabel({ id: "1", app_name: "x", accessed_at: "", source: "mcp", client_name: "unknown-client" }),
    ).toBe("MCP");
  });

  it("só ui:<UUID de sessão> identifica pessoa", () => {
    const base = { id: "1", app_name: "x", accessed_at: "" };
    expect(isAnonymousWebReader({ ...base, hostname: "ui:anonymous" })).toBe(true);
    expect(isAnonymousWebReader({ ...base, hostname: "ui:S0293" })).toBe(true);
    expect(isAnonymousWebReader({ ...base, hostname: "ui:openmemory" })).toBe(true);
    expect(isAnonymousWebReader({ ...base, hostname: "ui:0f8c2b8e-3a4d-4c1e-9b7a-2d5e6f708192" })).toBe(false);
    expect(isAnonymousWebReader({ ...base, hostname: "S0258" })).toBe(false);
  });

  it("trunca query e normaliza meta", () => {
    expect(truncateQuery("a".repeat(100), 10)).toBe(`${"a".repeat(9)}…`);
    expect(truncateQuery("  ")).toBeUndefined();
    const meta = toAccessLogMeta({ total: 2, page: 1, page_size: 10, logs: [] });
    expect(meta.rawTotal).toBe(2);
    expect(meta.groupingTruncated).toBe(false);
    expect(
      toAccessLogMeta({ total: 2, page: 1, page_size: 10, logs: [], grouping_truncated: true })
        .groupingTruncated,
    ).toBe(true);
    expect(meta.channelCounts).toEqual({ web: 0, mcp: 0, api: 0, other: 0 });
  });
});
