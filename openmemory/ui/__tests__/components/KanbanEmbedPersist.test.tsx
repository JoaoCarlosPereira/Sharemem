import React from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import axios from "axios";

jest.mock("axios");
jest.mock("@/hooks/useApiSessionReady", () => ({
  useApiSessionReady: () => true,
}));
jest.mock("@/lib/api-url", () => ({
  getApiUrl: () => "/api-proxy",
}));

import { KanbanEmbedCanvas } from "@/components/docs/KanbanEmbedCanvas";

const mockedAxios = axios as jest.Mocked<typeof axios>;

describe("KanbanEmbedCanvas persistence", () => {
  beforeEach(() => {
    mockedAxios.get.mockReset();
    mockedAxios.isAxiosError = jest.requireActual("axios").isAxiosError;
    sessionStorage.clear();
    window.history.replaceState({}, "", "/docs");
  });

  it("restaura o último quadro do sessionStorage ao montar em /docs", async () => {
    sessionStorage.setItem("mem0_kanban_last_board", "1833672064557385241");
    mockedAxios.get.mockResolvedValue({
      data: {
        board_id: "1833672064557385241",
        embed_url: "/planka/boards/1833672064557385241",
        access_token: "a.b.c",
      },
    });

    render(<KanbanEmbedCanvas />);

    await waitFor(() => {
      expect(mockedAxios.get).toHaveBeenCalledWith(
        "/api-proxy/api/v1/specs/kanban-boards/1833672064557385241",
      );
    });
    const iframe = (await screen.findByTestId(
      "kanban-board-canvas",
    )) as HTMLIFrameElement;
    expect(iframe.src).toContain("/planka/boards/1833672064557385241");
    expect(window.location.pathname).toBe("/docs/boards/1833672064557385241");
  });

  it("cai para kanban-home quando o quadro salvo não está mais mapeado", async () => {
    sessionStorage.setItem("mem0_kanban_last_board", "9999999999999999999");
    mockedAxios.get
      .mockRejectedValueOnce({
        isAxiosError: true,
        response: { status: 404, data: { detail: "Quadro Kanban não mapeado" } },
      })
      .mockResolvedValueOnce({
        data: { embed_url: "/planka/", access_token: "home.jwt" },
      });

    render(<KanbanEmbedCanvas />);

    await waitFor(() => {
      expect(mockedAxios.get).toHaveBeenCalledWith(
        "/api-proxy/api/v1/specs/kanban-boards/9999999999999999999",
      );
      expect(mockedAxios.get).toHaveBeenCalledWith(
        "/api-proxy/api/v1/specs/kanban-home",
      );
    });
    const iframe = (await screen.findByTestId(
      "kanban-home-canvas",
    )) as HTMLIFrameElement;
    expect(iframe.src).toContain("home.jwt");
    expect(sessionStorage.getItem("mem0_kanban_last_board")).toBeNull();
    expect(window.location.pathname).toBe("/docs");
    expect(screen.queryByTestId("kanban-home-error")).not.toBeInTheDocument();
  });

  it("postMessage de path atualiza URL sem novo GET do embed", async () => {
    mockedAxios.get.mockResolvedValue({
      data: { embed_url: "/planka/", access_token: "a.b.c" },
    });

    render(<KanbanEmbedCanvas />);
    await screen.findByTestId("kanban-home-canvas");
    const callsAfterMount = mockedAxios.get.mock.calls.length;

    act(() => {
      window.dispatchEvent(
        new MessageEvent("message", {
          origin: window.location.origin,
          data: {
            source: "mem0-kanban",
            type: "path",
            boardId: "1833672064557385241",
          },
        }),
      );
    });

    await waitFor(() => {
      expect(window.location.pathname).toBe("/docs/boards/1833672064557385241");
    });
    expect(sessionStorage.getItem("mem0_kanban_last_board")).toBe(
      "1833672064557385241",
    );
    expect(mockedAxios.get.mock.calls.length).toBe(callsAfterMount);
  });

  it("focus/visibility não dispara novo GET nem troca src do iframe", async () => {
    mockedAxios.get.mockResolvedValue({
      data: {
        board_id: "1833672064557385241",
        embed_url: "/planka/boards/1833672064557385241",
        access_token: "a.b.c",
      },
    });
    sessionStorage.setItem("mem0_kanban_last_board", "1833672064557385241");

    render(<KanbanEmbedCanvas />);
    const iframe = (await screen.findByTestId(
      "kanban-board-canvas",
    )) as HTMLIFrameElement;
    const srcBefore = iframe.src;
    const callsBefore = mockedAxios.get.mock.calls.length;

    act(() => {
      Object.defineProperty(document, "visibilityState", {
        configurable: true,
        get: () => "visible",
      });
      document.dispatchEvent(new Event("visibilitychange"));
      window.dispatchEvent(new Event("focus"));
    });

    expect(mockedAxios.get.mock.calls.length).toBe(callsBefore);
    expect(
      (screen.getByTestId("kanban-board-canvas") as HTMLIFrameElement).src,
    ).toBe(srcBefore);
  });

  it("evento de atualização renova o embed com um token novo", async () => {
    mockedAxios.get
      .mockResolvedValueOnce({
        data: { embed_url: "/planka/", access_token: "token.inicial.jwt" },
      })
      .mockResolvedValueOnce({
        data: { embed_url: "/planka/", access_token: "token.renovado.jwt" },
      });

    render(<KanbanEmbedCanvas />);
    const iframe = (await screen.findByTestId(
      "kanban-home-canvas",
    )) as HTMLIFrameElement;
    expect(iframe.src).toContain("token.inicial.jwt");

    act(() => {
      window.dispatchEvent(new Event("mem0-kanban-reload"));
    });

    await waitFor(() => {
      expect(mockedAxios.get).toHaveBeenCalledTimes(2);
      expect(
        (screen.getByTestId("kanban-home-canvas") as HTMLIFrameElement).src,
      ).toContain("token.renovado.jwt");
    });
  });

  it("postMessage auth-expired do iframe renova o embed com novo token", async () => {
    mockedAxios.get
      .mockResolvedValueOnce({
        data: { embed_url: "/planka/", access_token: "token.inicial.jwt" },
      })
      .mockResolvedValueOnce({
        data: { embed_url: "/planka/", access_token: "token.auto-renovado.jwt" },
      });

    render(<KanbanEmbedCanvas />);
    const iframe = (await screen.findByTestId(
      "kanban-home-canvas",
    )) as HTMLIFrameElement;
    expect(iframe.src).toContain("token.inicial.jwt");

    act(() => {
      window.dispatchEvent(
        new MessageEvent("message", {
          origin: window.location.origin,
          data: {
            source: "mem0-kanban",
            type: "auth-expired",
          },
        }),
      );
    });

    await waitFor(() => {
      expect(mockedAxios.get).toHaveBeenCalledTimes(2);
      expect(
        (screen.getByTestId("kanban-home-canvas") as HTMLIFrameElement).src,
      ).toContain("token.auto-renovado.jwt");
    });
  });
});
