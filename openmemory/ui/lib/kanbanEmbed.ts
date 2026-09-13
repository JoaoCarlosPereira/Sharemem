import axios from "axios";

/** Extrai o campo `detail` de respostas FastAPI (string ou objeto aninhado). */
export function kanbanErrorDetail(err: unknown): string | null {
  if (!axios.isAxiosError(err)) return null;
  const raw = err.response?.data?.detail;
  if (typeof raw === "string") return raw;
  if (raw && typeof raw === "object" && "detail" in raw) {
    const nested = (raw as { detail?: unknown }).detail;
    if (typeof nested === "string") return nested;
  }
  return null;
}

export function isUnmappedBoardError(err: unknown): boolean {
  if (!axios.isAxiosError(err)) return false;
  if (err.response?.status === 404) return true;
  const detail = kanbanErrorDetail(err);
  return Boolean(detail && detail.toLowerCase().includes("não mapeado"));
}

/** Mensagem legível para a UI — sem expor detalhes internos desnecessários. */
export function formatKanbanLoadError(err: unknown): string {
  if (axios.isAxiosError(err)) {
    const status = err.response?.status;
    const detail = kanbanErrorDetail(err);

    if (status === 502) {
      const upstream =
        typeof detail === "string" && detail.toLowerCase().includes("indispon")
          ? detail
          : "Serviço indisponível (API ou PLANKA)";
      return upstream;
    }
    if (status === 503 && detail) {
      return detail;
    }
    if (status === 403) {
      return detail || "Sem permissão para acessar este quadro";
    }
    if (status === 404) {
      return detail || "Quadro Kanban não encontrado";
    }
    if (status === 401) {
      return detail || "Sessão expirada — faça login novamente";
    }
    if (detail) return detail;
    if (!err.response) {
      return "Falha de rede ao carregar o Kanban";
    }
  }
  return "Falha ao carregar Kanban";
}
