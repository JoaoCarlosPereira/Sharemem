import type { AccessChannelFilter, AccessLogMeta } from "@/types/accessLog";

export type AccessLogFilterValue = AccessChannelFilter | null;

interface AccessLogFilterProps {
  value: AccessLogFilterValue;
  meta: AccessLogMeta | null;
  onChange: (value: AccessLogFilterValue) => void;
}

/**
 * Filtro por canal: "Todos" | "Agentes" (MCP + API) | "Interface Web".
 * "Agentes" esconde as próprias aberturas de página pela UI, que dominavam o log.
 */
export function AccessLogFilter({ value, meta, onChange }: AccessLogFilterProps) {
  const counts = meta?.channelCounts;
  const agents = counts ? counts.mcp + counts.api : undefined;
  const all = counts ? counts.web + counts.mcp + counts.api + counts.other : undefined;
  const options: { key: AccessLogFilterValue; label: string; count?: number }[] = [
    { key: null, label: "Todos", count: all },
    { key: "agents", label: "Agentes", count: agents },
    { key: "web", label: "Interface Web", count: counts?.web },
  ];
  return (
    <div className="flex flex-wrap gap-1" role="group" aria-label="Filtrar log de acesso por canal">
      {options.map((opt) => {
        const active = value === opt.key;
        return (
          <button
            key={opt.label}
            type="button"
            aria-pressed={active}
            onClick={() => onChange(opt.key)}
            className={`rounded px-2 py-0.5 text-xs transition-colors ${
              active ? "bg-zinc-700 text-zinc-100" : "text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200"
            }`}
          >
            {opt.label}
            {opt.count !== undefined && <span className="ml-1 tabular-nums text-zinc-500">{opt.count}</span>}
          </button>
        );
      })}
    </div>
  );
}

/**
 * Aviso de histórico parcial: a API só agrupa as N leituras mais recentes
 * (``ACCESS_LOG_GROUP_SCAN_LIMIT``); as mais antigas não aparecem na lista.
 */
export function AccessLogPartialNotice({ meta }: { meta: AccessLogMeta | null }) {
  if (!meta?.groupingTruncated) return null;
  return (
    <p
      role="status"
      data-testid="access-log-partial"
      className="rounded border border-amber-800/60 bg-amber-950/30 px-2 py-1 text-xs text-amber-300"
      title={`${meta.rawTotal} acessos registrados; só os mais recentes foram agrupados e listados`}
    >
      Histórico parcial: exibindo apenas os acessos mais recentes.
    </p>
  );
}
