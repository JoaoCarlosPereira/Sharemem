/**
 * Entrada do log de acesso de uma memória (GET /api/v1/memories/{id}/access-log).
 *
 * Campos marcados como opcionais são aditivos (card 01ada614): a API agrupa
 * leituras repetidas do mesmo ator/tipo numa janela (``count`` + intervalo) e
 * informa o canal (Interface Web vs MCP vs API compat).
 */
export type AccessChannel = "web" | "mcp" | "api" | "other";

export type AccessChannelFilter = "web" | "mcp" | "api" | "agents";

export interface AccessLogEntry {
  id: string;
  app_name: string;
  display_name?: string;
  avatar_url?: string;
  client_name?: string | null;
  hostname?: string;
  accessed_at: string;
  access_type?: string;
  source?: string;
  query?: string | null;
  channel?: AccessChannel;
  channel_label?: string;
  count?: number;
  first_accessed_at?: string;
  last_accessed_at?: string;
  /** Leitura da Interface Web sem sessão Google (sem pessoa/avatar). */
  anonymous?: boolean;
}

export interface AccessLogMeta {
  total: number;
  rawTotal: number;
  grouped: boolean;
  groupWindowSeconds: number;
  /** A API agrupou só as N leituras mais recentes (histórico parcial). */
  groupingTruncated: boolean;
  channel: AccessChannelFilter | null;
  channelCounts: Record<AccessChannel, number>;
}

export interface AccessLogResponse {
  total: number;
  page: number;
  page_size: number;
  logs: AccessLogEntry[];
  raw_total?: number;
  grouped?: boolean;
  group_window_seconds?: number;
  grouping_truncated?: boolean;
  channel?: AccessChannelFilter | null;
  channel_counts?: Partial<Record<AccessChannel, number>>;
}
