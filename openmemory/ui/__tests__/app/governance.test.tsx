import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { configureStore } from "@reduxjs/toolkit";
import { Provider } from "react-redux";

jest.mock("axios");
import axios from "axios";
const mockedAxios = axios as jest.Mocked<typeof axios>;

const fetchGovernanceJobs = jest.fn();
jest.mock("@/hooks/useQueuesApi", () => ({
  useQueuesApi: jest.fn(() => ({
    fetchGovernanceJobs,
    fetchWriteQueue: jest.fn(),
    refreshAll: jest.fn(),
  })),
}));
jest.mock("@/hooks/useAdminApi", () => ({
  useAdminApi: jest.fn(() => ({
    fetchProjectSizes: jest.fn().mockResolvedValue({
      threshold: 0,
      over_threshold_count: 0,
      projects: [
        {
          name: "proj-a",
          memory_count: 1,
          partition_tier: "shared",
          shard_key: null,
          over_threshold: false,
        },
      ],
    }),
  })),
}));
jest.mock("sonner", () => ({ toast: { success: jest.fn(), error: jest.fn() } }));

import adminReducer from "@/store/adminSlice";
import queuesReducer, { setGovernanceQueue } from "@/store/queuesSlice";
import GovernancePage from "@/app/admin/governance/page";
import type { GovernanceJob } from "@/types/admin";

const job: GovernanceJob = {
  id: "g1",
  job_type: "dedup",
  project: "proj-a",
  status: "queued",
  attempts: 0,
  error: null,
  created_at: "2026-01-01T10:00:00Z",
  updated_at: "2026-01-01T10:00:00Z",
};

const defaultProcesses = {
  dedup: true,
  ttl_prune: true,
  consolidate: false,
  purge: true,
  quality_eval: true,
  enforce_quota: true,
  cold_tier: true,
  merge_projects: true,
};

function renderPage() {
  const store = configureStore({
    reducer: { admin: adminReducer, queues: queuesReducer },
  });
  store.dispatch(
    setGovernanceQueue({
      items: [job],
      total: 1,
      page: 1,
      pages: 1,
      failed_count: 0,
    }),
  );
  return render(
    <Provider store={store}>
      <GovernancePage />
    </Provider>,
  );
}

beforeEach(() => {
  mockedAxios.get.mockImplementation((url: string) => {
    if (String(url).includes("/admin/governance/schedule")) {
      return Promise.resolve({
        data: {
          schedule_timezone: "UTC",
          schedule_weekdays: [0, 1, 2, 3, 4, 5, 6],
          schedule_start_time: "00:00",
          schedule_end_time: "23:59",
        },
      });
    }
    if (String(url).includes("/admin/governance/processes")) {
      return Promise.resolve({
        data: { processes_enabled: defaultProcesses },
      });
    }
    if (String(url).includes("/projects/merge-proposals")) {
      return Promise.resolve({
        data: {
          items: [
            {
              id: "p1",
              canonical: "sysmovs",
              aliases: ["dsv-sysmovs"],
              confidence: 0.9,
              reason: "mesmo repo",
              status: "pending",
            },
          ],
          count: 1,
        },
      });
    }
    return Promise.resolve({ data: { global: {} } });
  });
  mockedAxios.post.mockReset().mockResolvedValue({ data: { status: "queued" } });
  mockedAxios.put.mockReset().mockImplementation((url: string, body: unknown) => {
    if (String(url).includes("/admin/governance/processes")) {
      return Promise.resolve({ data: body });
    }
    return Promise.resolve({ data: body });
  });
  fetchGovernanceJobs.mockReset().mockResolvedValue(undefined);
});

describe("GovernancePage", () => {
  it("exibe botões para Deduplicar, TTL Prune, Consolidar e Purgar", () => {
    renderPage();
    ["Deduplicar", "TTL Prune", "Consolidar", "Purgar"].forEach((l) =>
      expect(screen.getByRole("button", { name: l })).toBeInTheDocument(),
    );
  });

  it("clicar em Deduplicar abre o dialog com select de projeto", async () => {
    renderPage();
    await userEvent.click(screen.getByRole("button", { name: "Deduplicar" }));
    expect(screen.getByText("Disparar Deduplicar")).toBeInTheDocument();
    expect(screen.getByLabelText("Selecionar projeto")).toBeInTheDocument();
  });

  it("clicar em Purgar abre dialog com aviso de operação destrutiva", async () => {
    renderPage();
    await userEvent.click(screen.getByRole("button", { name: "Purgar" }));
    expect(screen.getByText(/DESTRUTIVA/i)).toBeInTheDocument();
  });

  it("fechar o dialog sem confirmar não dispara POST", async () => {
    renderPage();
    await userEvent.click(screen.getByRole("button", { name: "Deduplicar" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancelar" }));
    expect(mockedAxios.post).not.toHaveBeenCalled();
  });

  it("confirmar o disparo chama POST /admin/governance/jobs/dedup e recarrega", async () => {
    renderPage();
    await userEvent.click(screen.getByRole("button", { name: "Deduplicar" }));
    await userEvent.click(
      screen.getByRole("button", { name: "Confirmar disparo" }),
    );
    await waitFor(() =>
      expect(mockedAxios.post).toHaveBeenCalledWith(
        expect.stringContaining("/admin/governance/jobs/dedup"),
        expect.objectContaining({ project: null }),
      ),
    );
    await waitFor(() => expect(fetchGovernanceJobs).toHaveBeenCalled());
  });

  it("seção de políticas é somente leitura (sem inputs editáveis)", () => {
    renderPage();
    expect(
      screen.getByText("Políticas ativas (somente leitura)"),
    ).toBeInTheDocument();
    // A política bruta continua sem textarea/editor JSON.
    expect(screen.queryByRole("textbox", { name: /política/i })).not.toBeInTheDocument();
  });

  it("a lista de governance jobs usa JobStatusBadge", () => {
    renderPage();
    expect(screen.getByText("Na fila")).toHaveClass("bg-zinc-700");
  });


  it("exibe um switch para cada processo de governança", async () => {
    renderPage();
    for (const label of [
      "Deduplicação",
      "TTL Prune",
      "Consolidação",
      "Purga",
      "Avaliação de qualidade",
      "Cota de memórias",
      "Cold tier",
      "Unificação de projetos",
    ]) {
      expect(
        await screen.findByRole("switch", { name: `Habilitar ${label}` }),
      ).toBeInTheDocument();
    }
  });

  it("persiste a desativação de um processo", async () => {
    renderPage();
    const switchDedup = await screen.findByRole("switch", {
      name: "Habilitar Deduplicação",
    });
    await userEvent.click(switchDedup);

    await waitFor(() =>
      expect(mockedAxios.put).toHaveBeenCalledWith(
        expect.stringContaining("/admin/governance/processes"),
        expect.objectContaining({
          processes_enabled: expect.objectContaining({ dedup: false }),
        }),
      ),
    );
  });

  it("desabilita ações manuais dos processos desligados", async () => {
    renderPage();
    expect(
      await screen.findByRole("button", { name: "Consolidar" }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "Analisar Duplicatas" }),
    ).toBeEnabled();
  });

  it("lista propostas de unificação e aprova via POST", async () => {
    renderPage();
    expect(await screen.findByText("sysmovs")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Aprovar" }));
    await waitFor(() =>
      expect(mockedAxios.post).toHaveBeenCalledWith(
        expect.stringContaining("/projects/merge-proposals/p1/approve"),
        {},
      ),
    );
  });

});
