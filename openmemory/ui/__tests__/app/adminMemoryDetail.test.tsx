import React from "react";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Provider } from "react-redux";
import { configureStore } from "@reduxjs/toolkit";
import memoriesReducer, {
  setAccessLogMeta,
  setAccessLogs,
  setSelectedMemory,
} from "@/store/memoriesSlice";
import profileReducer, { setApiSessionStatus } from "@/store/profileSlice";
import type { ApiSessionStatus } from "@/store/profileSlice";

jest.mock("next/image", () => ({
  __esModule: true,
  default: ({ unoptimized: _u, ...props }: Record<string, unknown>) => (
    // eslint-disable-next-line @next/next/no-img-element, jsx-a11y/alt-text
    <img {...(props as object)} />
  ),
}));
jest.mock("next/navigation", () => ({
  useParams: () => ({ project: "sysmovs", memoryId: "mem-1" }),
}));

const fetchAccessLogs = jest.fn();
const fetchMemoryById = jest.fn();
jest.mock("@/hooks/useMemoriesApi", () => ({
  useMemoriesApi: () => ({ fetchAccessLogs, fetchMemoryById, isLoading: false }),
}));

import AdminMemoryDetailPage from "@/app/admin/projects/[project]/[memoryId]/page";

function setup(sessionStatus: ApiSessionStatus = "valid") {
  const store = configureStore({
    reducer: { memories: memoriesReducer, profile: profileReducer },
  });
  store.dispatch(setApiSessionStatus(sessionStatus));
  fetchMemoryById.mockImplementation(async () => {
    store.dispatch(
      setSelectedMemory({
        id: "mem-1",
        text: "conteúdo",
        created_at: "2026-10-02",
        state: "active",
        categories: [],
        app_name: "sysmovs",
      } as never),
    );
  });
  fetchAccessLogs.mockImplementation(async () => {
    store.dispatch(
      setAccessLogs([
        {
          id: "a",
          app_name: "openmemory",
          display_name: "Ana Souza",
          hostname: "ui:0f8c2b8e-3a4d-4c1e-9b7a-2d5e6f708192",
          source: "api",
          access_type: "list",
          channel: "web",
          count: 3,
          accessed_at: "2026-10-02T18:14:43+00:00",
          first_accessed_at: "2026-10-02T18:12:00+00:00",
          last_accessed_at: "2026-10-02T18:14:43+00:00",
        },
        {
          id: "b",
          app_name: "claude-code",
          display_name: "Bruno Lima",
          client_name: "claude-code",
          hostname: "S0258",
          source: "mcp",
          access_type: "search",
          channel: "mcp",
          query: "frete",
          accessed_at: "2026-10-01T17:00:50+00:00",
        },
      ]),
    );
    store.dispatch(
      setAccessLogMeta({
        total: 2,
        rawTotal: 4,
        grouped: true,
        groupWindowSeconds: 300,
        groupingTruncated: true,
        channel: null,
        channelCounts: { web: 3, mcp: 1, api: 0, other: 0 },
      }),
    );
  });
  render(
    <Provider store={store}>
      <AdminMemoryDetailPage />
    </Provider>,
  );
  return store;
}

beforeEach(() => {
  fetchAccessLogs.mockReset();
  fetchMemoryById.mockReset();
});

describe("AdminMemoryDetailPage — Últimos acessos", () => {
  it("exibe pessoa, canal, tipo e contagem por entrada", async () => {
    setup();
    const items = await screen.findAllByTestId("access-log-entry");
    expect(items).toHaveLength(2);
    expect(within(items[0]).getByText("Ana Souza")).toBeInTheDocument();
    expect(within(items[0]).getByText("3×")).toBeInTheDocument();
    expect(within(items[0]).getByText("Interface Web")).toBeInTheDocument();
    expect(within(items[0]).getByText("Listagem")).toBeInTheDocument();
    expect(within(items[1]).getByText("Bruno Lima")).toBeInTheDocument();
    expect(within(items[1]).getByText("MCP · Claude Code")).toBeInTheDocument();
    expect(within(items[1]).getByText("Busca")).toBeInTheDocument();
    expect(screen.getByTestId("access-log-partial")).toHaveTextContent(/Histórico parcial/);
  });

  it("filtro por canal refaz a busca", async () => {
    setup();
    await screen.findAllByTestId("access-log-entry");
    expect(fetchAccessLogs).toHaveBeenLastCalledWith("mem-1", 1, 20, { channel: null });
    await userEvent.click(screen.getByRole("button", { name: /Interface Web/ }));
    await waitFor(() =>
      expect(fetchAccessLogs).toHaveBeenLastCalledWith("mem-1", 1, 20, { channel: "web" }),
    );
  });

  it("espera a sessão ficar pronta antes de ler (I1) e lê uma única vez (I2)", async () => {
    const store = setup("validating");
    expect(fetchMemoryById).not.toHaveBeenCalled();
    expect(fetchAccessLogs).not.toHaveBeenCalled();
    act(() => {
      store.dispatch(setApiSessionStatus("valid"));
    });
    await screen.findAllByTestId("access-log-entry");
    expect(fetchMemoryById).toHaveBeenCalledTimes(1);
    expect(fetchMemoryById).toHaveBeenCalledWith("mem-1");
  });
});
