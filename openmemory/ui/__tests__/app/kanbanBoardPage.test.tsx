import React from "react";
import { render, screen, waitFor, act } from "@testing-library/react";
import axios from "axios";

jest.mock("axios");
jest.mock("@/hooks/useApiSessionReady", () => ({
  useApiSessionReady: () => true,
}));
jest.mock("@/lib/api-url", () => ({
  getApiUrl: () => "/api-proxy",
}));

import KanbanBoardPage from "@/app/docs/boards/[boardId]/page";

const mockedAxios = axios as jest.Mocked<typeof axios>;

describe("KanbanBoardPage deep-link", () => {
  beforeEach(() => {
    mockedAxios.get.mockReset();
    mockedAxios.isAxiosError = jest.requireActual("axios").isAxiosError;
  });

  it("carrega embed do quadro via kanban-boards/:id", async () => {
    mockedAxios.get.mockResolvedValue({
      data: {
        board_id: "1833672064557385241",
        embed_url: "/planka/boards/1833672064557385241",
        access_token: "a.b.c",
      },
    });

    await act(async () => {
      render(
        <KanbanBoardPage
          params={Promise.resolve({ boardId: "1833672064557385241" })}
        />,
      );
    });

    await waitFor(() => {
      expect(mockedAxios.get).toHaveBeenCalledWith(
        "/api-proxy/api/v1/specs/kanban-boards/1833672064557385241",
      );
    }, { timeout: 2000 });

    const iframe = (await screen.findByTestId(
      "kanban-board-canvas",
    )) as HTMLIFrameElement;
    expect(iframe.src).toContain("/planka/boards/1833672064557385241");
    expect(iframe.src).toContain("mem0_token=a.b.c");
    expect(iframe.src).toContain("embed=1");
  });

  it("deep-link inválido mostra erro e botão para home", async () => {
    mockedAxios.get.mockRejectedValue({
      isAxiosError: true,
      response: { status: 404, data: { detail: "Quadro Kanban não mapeado" } },
    });

    await act(async () => {
      render(
        <KanbanBoardPage
          params={Promise.resolve({ boardId: "9999999999999999999" })}
        />,
      );
    });

    await waitFor(() => {
      expect(screen.getByTestId("kanban-home-error")).toBeInTheDocument();
      expect(screen.getByRole("alert")).toHaveTextContent(
        "Quadro Kanban não mapeado",
      );
    });
    expect(
      screen.getByRole("button", { name: /ir para home do kanban/i }),
    ).toBeInTheDocument();
    expect(mockedAxios.get).not.toHaveBeenCalledWith(
      "/api-proxy/api/v1/specs/kanban-home",
    );
  });
});
