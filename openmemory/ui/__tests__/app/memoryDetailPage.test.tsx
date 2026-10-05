import React, { Suspense } from "react";
import { act, render, screen } from "@testing-library/react";
import { Provider } from "react-redux";
import { configureStore } from "@reduxjs/toolkit";
import memoriesReducer, { setSelectedMemory } from "@/store/memoriesSlice";
import profileReducer, { setApiSessionStatus } from "@/store/profileSlice";
import uiReducer from "@/store/uiSlice";

const fetchMemoryById = jest.fn();
jest.mock("@/hooks/useMemoriesApi", () => ({
  useMemoriesApi: () => ({ fetchMemoryById, isLoading: false, error: null }),
}));
jest.mock("@/components/shared/update-memory", () => ({
  __esModule: true,
  default: () => null,
}));
// MemoryDetails só exibe; se voltasse a buscar, a contagem abaixo pegaria.
jest.mock("@/app/memory/[id]/components/MemoryDetails", () => ({
  MemoryDetails: ({ memory_id }: { memory_id: string }) => (
    <div data-testid="memory-details">{memory_id}</div>
  ),
}));

import MemoryPage from "@/app/memory/[id]/page";

function renderPage(status: "idle" | "validating" | "valid" | "invalid") {
  const store = configureStore({
    reducer: { memories: memoriesReducer, profile: profileReducer, ui: uiReducer },
  });
  store.dispatch(setApiSessionStatus(status));
  fetchMemoryById.mockImplementation(async (id: string) => {
    store.dispatch(
      setSelectedMemory({
        id,
        text: "conteúdo",
        created_at: "2026-10-02",
        state: "active",
        categories: [],
        app_name: "sysmovs",
      } as never),
    );
  });
  // Promise já resolvida no formato que o `use()` do React 19 lê sem suspender.
  const params = Object.assign(Promise.resolve({ id: "mem-1" }), {
    status: "fulfilled",
    value: { id: "mem-1" },
  });
  const view = render(
    <Provider store={store}>
      <Suspense fallback={null}>
        <MemoryPage params={params} />
      </Suspense>
    </Provider>,
  );
  return { store, view };
}

beforeEach(() => fetchMemoryById.mockReset());

describe("MemoryPage — leitura auditada", () => {
  it("não lê enquanto a sessão não está decidida e lê uma vez depois (I1/I2)", async () => {
    const { store } = renderPage("validating");
    await act(async () => {});
    expect(fetchMemoryById).not.toHaveBeenCalled();
    expect(screen.queryByTestId("memory-details")).not.toBeInTheDocument();

    await act(async () => {
      store.dispatch(setApiSessionStatus("valid"));
    });
    expect(await screen.findByTestId("memory-details")).toHaveTextContent("mem-1");
    expect(fetchMemoryById).toHaveBeenCalledTimes(1);
    expect(fetchMemoryById).toHaveBeenCalledWith("mem-1");
  });

  it("sem login (sessão invalid) também lê uma única vez", async () => {
    renderPage("invalid");
    expect(await screen.findByTestId("memory-details")).toBeInTheDocument();
    expect(fetchMemoryById).toHaveBeenCalledTimes(1);
  });
});
