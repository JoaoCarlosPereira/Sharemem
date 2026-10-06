import { Bot, Eye, Globe, List, Search, Server, type LucideIcon } from "lucide-react";
import { resolveAttribution } from "@/lib/attribution";
import {
  ANONYMOUS_WEB_LABEL,
  accessChannelLabel,
  accessCount,
  accessTypeLabel,
  isAnonymousWebReader,
  resolveAccessChannel,
  truncateQuery,
} from "@/lib/access-log";
import { formatDateTime, formatDateTimeShort } from "@/lib/i18n/pt-BR";
import { CreatorAvatar } from "@/components/shared/creator-avatar";
import type { AccessChannel, AccessLogEntry } from "@/types/accessLog";

const TYPE_ICONS: Record<string, LucideIcon> = {
  get: Eye,
  search: Search,
  list: List,
};

const CHANNEL_ICONS: Record<AccessChannel, LucideIcon> = {
  web: Globe,
  mcp: Bot,
  api: Server,
  other: Server,
};

const CHANNEL_STYLES: Record<AccessChannel, string> = {
  web: "border-zinc-700 text-zinc-400",
  mcp: "border-sky-800 text-sky-300",
  api: "border-violet-800 text-violet-300",
  other: "border-zinc-700 text-zinc-400",
};

function formatTime(value?: string): string {
  if (!value) return "";
  return formatDateTimeShort(value).split(" ")[1] ?? "";
}

/** Intervalo de um grupo: "15:10–15:14" (mesmo dia) ou data/hora completa. */
export function accessInterval(entry: AccessLogEntry): string | undefined {
  if (accessCount(entry) <= 1 || !entry.first_accessed_at) return undefined;
  const last = entry.last_accessed_at || entry.accessed_at;
  const firstDay = formatDateTimeShort(entry.first_accessed_at).split(" ")[0];
  const lastDay = formatDateTimeShort(last).split(" ")[0];
  if (firstDay === lastDay) {
    return `${formatTime(entry.first_accessed_at)}–${formatTime(last)}`;
  }
  return `${formatDateTimeShort(entry.first_accessed_at)} – ${formatDateTimeShort(last)}`;
}

interface AccessLogEntryViewProps {
  entry: AccessLogEntry;
  variant?: "timeline" | "compact";
}

/**
 * Uma entrada do log de acesso: pessoa (nome + foto), canal (Interface Web /
 * MCP · cliente / API), tipo (Leitura/Busca/Listagem), contagem agrupada e
 * query truncada.
 */
export function AccessLogEntryView({ entry, variant = "timeline" }: AccessLogEntryViewProps) {
  const anonymous = isAnonymousWebReader(entry);
  const attribution = resolveAttribution({
    appName: entry.app_name,
    clientName: entry.client_name,
    hostname: entry.hostname,
    displayName: anonymous ? ANONYMOUS_WEB_LABEL : entry.display_name,
    // Leitura sem sessão: nunca mostrar foto de pessoa.
    avatarUrl: anonymous ? undefined : entry.avatar_url,
  });
  const channel = resolveAccessChannel(entry);
  const ChannelIcon = CHANNEL_ICONS[channel];
  const typeKey = (entry.access_type || "").toLowerCase();
  const TypeIcon = TYPE_ICONS[typeKey] ?? Eye;
  const count = accessCount(entry);
  const interval = accessInterval(entry);
  const query = truncateQuery(entry.query);
  const avatarSize = variant === "compact" ? 24 : 32;

  return (
    <div className="flex min-w-0 items-start gap-3" data-testid="access-log-entry">
      <div
        className="relative z-10 flex flex-shrink-0 items-center justify-center overflow-hidden rounded-full bg-[#2a2a2a]"
        style={{ width: avatarSize, height: avatarSize }}
      >
        <CreatorAvatar attribution={attribution} size={avatarSize} className="h-full w-full" />
      </div>
      <div className="flex min-w-0 flex-col gap-0.5">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className="font-medium text-zinc-100">{attribution.label}</span>
          {count > 1 && (
            <span
              className="rounded bg-zinc-800 px-1.5 text-xs font-medium tabular-nums text-zinc-300"
              title={`${count} acessos agrupados`}
            >
              {count}×
            </span>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-1.5 text-xs">
          <span
            className={`inline-flex items-center gap-1 rounded border px-1.5 py-px ${CHANNEL_STYLES[channel]}`}
            data-channel={channel}
          >
            <ChannelIcon className="h-3 w-3" aria-hidden />
            {accessChannelLabel(entry)}
          </span>
          <span className="inline-flex items-center gap-1 text-zinc-400" data-access-type={typeKey || "unknown"}>
            <TypeIcon className="h-3 w-3" aria-hidden />
            {accessTypeLabel(entry.access_type)}
          </span>
        </div>
        {query && (
          <span className="truncate text-xs italic text-zinc-500" title={entry.query || undefined}>
            “{query}”
          </span>
        )}
        <span className="text-sm text-zinc-400">
          {interval ? `${interval} · ${formatDateTime(entry.accessed_at)}` : formatDateTime(entry.accessed_at)}
        </span>
      </div>
    </div>
  );
}
