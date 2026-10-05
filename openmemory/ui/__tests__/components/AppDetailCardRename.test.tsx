import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { configureStore } from "@reduxjs/toolkit";
import { Provider } from "react-redux";

const renameApp = jest.fn();
jest.mock("@/hooks/useAppsApi", () => ({
  useAppsApi: () => ({
    renameApp,
    updateAppDetails: jest.fn(),
    deleteApp: jest.fn(),
  }),
}));
const push = jest.fn();
jest.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));
jest.mock("sonner", () => ({
  toast: { success: jest.fn(), info: jest.fn(), error: jest.fn() },
}));
jest.mock("@/components/shared/RenameProjectDialog", () => ({
  RenameProjectDialog: ({ onConfirm }: { onConfirm: (n: string) => void }) => (
    <button onClick={() => onConfirm("destino")}>confirmar-rename</button>
  ),
}));

import { toast } from "sonner";
import appsReducer from "@/store/appsSlice";
import AppDetailCard from "@/app/apps/[appId]/components/AppDetailCard";

const selectedApp = {
  details: {
    is_active: true,
    total_memories_created: 3,
    total_memories_accessed: 0,
    first_accessed: null,
    last_accessed: null,
  },
};

function renderCard() {
  const store = configureStore({ reducer: { apps: appsReducer } });
  return render(
    <Provider store={store}>
      <AppDetailCard appId="app-1" selectedApp={selectedApp} />
    </Provider>,
  );
}

beforeEach(() => {
  renameApp.mockReset();
  push.mockReset();
  jest.mocked(toast.success).mockReset();
  jest.mocked(toast.info).mockReset();
});

describe("AppDetailCard rename", () => {
  it("202 proposal_pending: avisa que aguarda aprovação e não redireciona", async () => {
    renameApp.mockResolvedValue({
      status: "proposal_pending",
      new_name: "destino",
      proposal: { id: "p1" },
    });
    renderCard();
    await userEvent.click(screen.getByText("confirmar-rename"));
    await waitFor(() => expect(toast.info).toHaveBeenCalled());
    expect(jest.mocked(toast.info).mock.calls[0][0]).toMatch(
      /proposta de unificação criada, aguarda aprovação em Governança/,
    );
    expect(toast.success).not.toHaveBeenCalled();
    expect(push).not.toHaveBeenCalled();
  });

  it("200 success: mostra memórias movidas e redireciona", async () => {
    renameApp.mockResolvedValue({
      status: "success",
      new_name: "destino",
      moved_memories: 3,
    });
    renderCard();
    await userEvent.click(screen.getByText("confirmar-rename"));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/apps"));
    expect(jest.mocked(toast.success).mock.calls[0][0]).toContain(
      "3 memórias",
    );
  });
});
