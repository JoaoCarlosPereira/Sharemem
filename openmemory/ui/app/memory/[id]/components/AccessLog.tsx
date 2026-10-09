import { useEffect, useState } from "react";
import { useMemoriesApi } from "@/hooks/useMemoriesApi";
import { useSelector } from "react-redux";
import { RootState } from "@/store/store";
import { ScrollArea } from "@/components/ui/scroll-area";
import { AccessLogEntryView } from "@/components/shared/access-log-entry";
import {
  AccessLogFilter,
  AccessLogPartialNotice,
  type AccessLogFilterValue,
} from "@/components/shared/access-log-filter";
import type { AccessLogEntry } from "@/types/accessLog";

const PAGE_SIZE = 10;

interface AccessLogProps {
  memoryId: string;
}

export function AccessLog({ memoryId }: AccessLogProps) {
  const { fetchAccessLogs } = useMemoriesApi();
  const accessEntries = useSelector(
    (state: RootState) => state.memories.accessLogs
  );
  const meta = useSelector((state: RootState) => state.memories.accessLogMeta);
  const [isLoading, setIsLoading] = useState(true);
  const [channel, setChannel] = useState<AccessLogFilterValue>(null);
  const [page, setPage] = useState(1);

  useEffect(() => {
    if (!memoryId) return;
    let cancelled = false;
    const loadAccessLogs = async () => {
      try {
        await fetchAccessLogs(memoryId, page, PAGE_SIZE, { channel });
      } catch (error) {
        console.error("Failed to fetch access logs:", error);
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    };

    loadAccessLogs();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [memoryId, channel, page]);

  const onChannelChange = (value: AccessLogFilterValue) => {
    setChannel(value);
    setPage(1);
  };

  const totalPages = meta ? Math.max(1, Math.ceil(meta.total / PAGE_SIZE)) : 1;

  if (isLoading) {
    return (
      <div className="w-full max-w-md mx-auto rounded-3xl overflow-hidden bg-[#1c1c1c] text-white p-6">
        <p className="text-center text-zinc-500">Carregando logs de acesso...</p>
      </div>
    );
  }

  return (
    <div className="w-full max-w-md mx-auto rounded-lg overflow-hidden bg-zinc-900 border border-zinc-800 text-white pb-1">
      <div className="px-6 py-4 flex flex-col gap-2 bg-zinc-800 border-b border-zinc-800">
        <div className="flex justify-between items-center">
          <h2 className="font-semibold">Log de Acesso</h2>
          {meta && meta.rawTotal > meta.total && (
            <span
              className="text-xs text-zinc-400 tabular-nums"
              title={`Leituras repetidas da mesma pessoa e tipo em até ${Math.round(meta.groupWindowSeconds / 60)} min são agrupadas`}
            >
              {meta.rawTotal} acessos · {meta.total} grupos
            </span>
          )}
        </div>
        <AccessLogFilter value={channel} meta={meta} onChange={onChannelChange} />
        <AccessLogPartialNotice meta={meta} />
      </div>

      <ScrollArea className="p-6 max-h-[450px]">
        {accessEntries.length === 0 && (
          <div className="w-full max-w-md mx-auto rounded-3xl overflow-hidden min-h-[110px] flex items-center justify-center text-white p-6">
            <p className="text-center text-zinc-500">
              {channel ? "Nenhum acesso neste filtro" : "Nenhum log de acesso disponível"}
            </p>
          </div>
        )}
        <ul className="space-y-6">
          {accessEntries.map((entry: AccessLogEntry, index: number) => (
            <li key={entry.id} className="relative">
              {index < accessEntries.length - 1 && (
                <div className="absolute left-4 top-6 bottom-0 w-[1px] h-[calc(100%+1rem)] bg-[#333333] transform -translate-x-1/2"></div>
              )}
              <AccessLogEntryView entry={entry} />
            </li>
          ))}
        </ul>
      </ScrollArea>

      {totalPages > 1 && (
        <div className="flex items-center justify-between px-6 py-2 text-xs text-zinc-400">
          <button
            type="button"
            disabled={page <= 1}
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            className="disabled:opacity-40 hover:text-zinc-200"
          >
            Mais recentes
          </button>
          <span className="tabular-nums">
            {page} / {totalPages}
          </span>
          <button
            type="button"
            disabled={page >= totalPages}
            onClick={() => setPage((p) => p + 1)}
            className="disabled:opacity-40 hover:text-zinc-200"
          >
            Mais antigos
          </button>
        </div>
      )}
    </div>
  );
}
