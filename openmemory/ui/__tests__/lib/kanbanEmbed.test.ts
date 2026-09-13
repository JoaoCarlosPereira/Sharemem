import axios from "axios";

import {
  formatKanbanLoadError,
  isUnmappedBoardError,
  kanbanErrorDetail,
} from "@/lib/kanbanEmbed";

describe("kanbanEmbed helpers", () => {
  it("detecta quadro não mapeado (404)", () => {
    const err = {
      isAxiosError: true,
      response: { status: 404, data: { detail: "Quadro Kanban não mapeado" } },
    };
    expect(isUnmappedBoardError(err)).toBe(true);
    expect(formatKanbanLoadError(err)).toBe("Quadro Kanban não mapeado");
  });

  it("formata 403, 503 e rede", () => {
    expect(
      formatKanbanLoadError({
        isAxiosError: true,
        response: { status: 403, data: { detail: "Sem permissão" } },
      }),
    ).toBe("Sem permissão");
    expect(
      formatKanbanLoadError({
        isAxiosError: true,
        response: {
          status: 503,
          data: { detail: "AUTH_JWT_SECRET necessário para embed Kanban" },
        },
      }),
    ).toBe("AUTH_JWT_SECRET necessário para embed Kanban");
    expect(
      formatKanbanLoadError({
        isAxiosError: true,
        message: "Network Error",
        response: undefined,
      }),
    ).toBe("Falha de rede ao carregar o Kanban");
  });

  it("extrai detail aninhado", () => {
    const err = {
      isAxiosError: true,
      response: { status: 502, data: { detail: { detail: "upstream indisponível" } } },
    };
    expect(kanbanErrorDetail(err)).toBe("upstream indisponível");
    expect(formatKanbanLoadError(err)).toBe("upstream indisponível");
  });

  it("ignora erros não-axios", () => {
    expect(isUnmappedBoardError(new Error("x"))).toBe(false);
    expect(formatKanbanLoadError(new Error("x"))).toBe("Falha ao carregar Kanban");
  });
});

// axios.isAxiosError é usado pelos helpers
beforeAll(() => {
  (axios as { isAxiosError?: (v: unknown) => boolean }).isAxiosError = (
    v: unknown,
  ) => Boolean(v && typeof v === "object" && (v as { isAxiosError?: boolean }).isAxiosError);
});
