import { constants } from "@/components/shared/source-app";
import type {
  AccessChannel,
  AccessLogEntry,
  AccessLogMeta,
  AccessLogResponse,
} from "@/types/accessLog";

/** Rótulo PT-BR do tipo de acesso registrado em ``read_audit_logs``. */
export const ACCESS_TYPE_LABELS: Record<string, string> = {
  get: "Leitura",
  search: "Busca",
  list: "Listagem",
};

export function accessTypeLabel(type?: string | null): string {
  const key = (type || "").trim().toLowerCase();
  return ACCESS_TYPE_LABELS[key] || (key ? key : "Acesso");
}

const CHANNEL_LABELS: Record<AccessChannel, string> = {
  web: "Interface Web",
  mcp: "MCP",
  api: "API",
  other: "Outro",
};

/** Canal do acesso; compatível com respostas antigas (sem ``channel``). */
export function resolveAccessChannel(entry: AccessLogEntry): AccessChannel {
  if (entry.channel) return entry.channel;
  const source = (entry.source || "").toLowerCase();
  if (source === "api" || source === "admin" || entry.hostname?.startsWith("ui:")) {
    return "web";
  }
  if (source === "mcp") return "mcp";
  if (source === "compat_v3") return "api";
  return "other";
}

function clientDisplayName(client?: string | null): string | undefined {
  const key = (client || "").trim().toLowerCase();
  if (!key || key === "unknown-client" || key === "unknown" || key === "openmemory") {
    return undefined;
  }
  const known = constants[key as keyof typeof constants];
  if (known) return known.name;
  if (key === "claude-code" || key === "claude_code") return "Claude Code";
  return client!.trim();
}

/** "Interface Web" | "MCP · Claude Code" | "API · cursor" */
export function accessChannelLabel(entry: AccessLogEntry): string {
  const channel = resolveAccessChannel(entry);
  if (channel === "web") return entry.channel_label || CHANNEL_LABELS.web;
  const base = channel === "other" ? entry.channel_label || CHANNEL_LABELS.other : CHANNEL_LABELS[channel];
  const client = clientDisplayName(entry.client_name);
  return client ? `${base} · ${client}` : base;
}

export const ANONYMOUS_WEB_LABEL = "Interface Web (sem login)";

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * Leitura da Interface Web sem sessão Google. Só ``ui:<User.id>`` (UUID do JWT)
 * identifica uma pessoa; ``ui:anonymous`` e linhas antigas com o ID fixo do
 * build (ex.: ``ui:S0293``) não podem virar nome/foto de ninguém.
 */
export function isAnonymousWebReader(entry: AccessLogEntry): boolean {
  if (entry.anonymous) return true;
  // Só leituras da Interface Web têm sessão; MCP/API gravam hostname declarado.
  if (resolveAccessChannel(entry) !== "web") return false;
  const host = (entry.hostname || "").trim();
  if (!host.startsWith("ui:")) return false;
  return !UUID_RE.test(host.slice(3).trim());
}

export function truncateQuery(query?: string | null, max = 80): string | undefined {
  const text = (query || "").replace(/\s+/g, " ").trim();
  if (!text) return undefined;
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

export function accessCount(entry: AccessLogEntry): number {
  return entry.count && entry.count > 0 ? entry.count : 1;
}

export function toAccessLogMeta(data: AccessLogResponse): AccessLogMeta {
  const counts = data.channel_counts || {};
  return {
    total: data.total,
    rawTotal: data.raw_total ?? data.total,
    grouped: Boolean(data.grouped),
    groupWindowSeconds: data.group_window_seconds ?? 0,
    groupingTruncated: Boolean(data.grouping_truncated),
    channel: data.channel ?? null,
    channelCounts: {
      web: counts.web ?? 0,
      mcp: counts.mcp ?? 0,
      api: counts.api ?? 0,
      other: counts.other ?? 0,
    },
  };
}
