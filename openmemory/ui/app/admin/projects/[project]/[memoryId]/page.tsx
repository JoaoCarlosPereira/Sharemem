"use client";

import { useEffect, useRef, useState } from "react";
import { useApiSessionSettled } from "@/hooks/useApiSessionReady";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useSelector } from "react-redux";
import { RootState } from "@/store/store";
import { useMemoriesApi } from "@/hooks/useMemoriesApi";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { ArrowLeft } from "lucide-react";
import { resolveAttribution } from "@/lib/attribution";
import { AccessLogEntryView } from "@/components/shared/access-log-entry";
import {
  AccessLogFilter,
  AccessLogPartialNotice,
  type AccessLogFilterValue,
} from "@/components/shared/access-log-filter";

export default function AdminMemoryDetailPage() {
  const params = useParams<{ project: string; memoryId: string }>();
  const { project, memoryId } = params;
  const projectName = decodeURIComponent(project);
  const { fetchMemoryById, fetchAccessLogs, isLoading } = useMemoriesApi();
  const memory = useSelector(
    (state: RootState) => state.memories.selectedMemory,
  );
  const accessLogs = useSelector(
    (state: RootState) => state.memories.accessLogs,
  );
  const accessLogMeta = useSelector(
    (state: RootState) => state.memories.accessLogMeta,
  );
  const [channel, setChannel] = useState<AccessLogFilterValue>(null);

  const sessionSettled = useApiSessionSettled();
  // Uma leitura auditada por abertura, só com a sessão decidida (Bearer anexado).
  const fetchedId = useRef<string | null>(null);

  useEffect(() => {
    if (!sessionSettled || fetchedId.current === memoryId) return;
    fetchedId.current = memoryId;
    fetchMemoryById(memoryId).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [memoryId, sessionSettled]);

  useEffect(() => {
    if (!sessionSettled) return;
    fetchAccessLogs(memoryId, 1, 20, { channel }).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [memoryId, channel, sessionSettled]);

  return (
    <div className="max-w-3xl">
      <Link
        href={`/admin/projects/${encodeURIComponent(projectName)}`}
        className="mb-4 inline-flex items-center gap-1 text-sm text-zinc-400 hover:text-zinc-200"
      >
        <ArrowLeft className="h-4 w-4" /> Voltar para {projectName}
      </Link>

      {isLoading && !memory ? (
        <Skeleton className="h-40 w-full rounded-lg" />
      ) : !memory ? (
        <p className="text-zinc-500">Memória não encontrada.</p>
      ) : (
        <div className="space-y-4">
          <Card className="border-zinc-800 bg-zinc-900">
            <CardHeader>
              <CardTitle className="text-zinc-100">Conteúdo</CardTitle>
            </CardHeader>
            <CardContent>
              <p className="whitespace-pre-wrap text-zinc-200">
                {memory.text}
              </p>
            </CardContent>
          </Card>

          <Card className="border-zinc-800 bg-zinc-900">
            <CardHeader>
              <CardTitle className="text-zinc-100">Metadados</CardTitle>
            </CardHeader>
            <CardContent className="space-y-1 text-sm text-zinc-400">
              <div>ID: {memory.id}</div>
              <div>Estado: {memory.state}</div>
              <div>
                Criado por:{" "}
                {resolveAttribution({
                  appName: memory.app_name,
                  clientName: memory.created_by_client,
                  hostname: memory.created_by_hostname,
                  displayName: memory.created_by_display_name,
                  avatarUrl: memory.created_by_avatar_url,
                  metadata: memory.metadata_,
                }).label}
              </div>
              <div>Categorias: {memory.categories?.join(", ") || "—"}</div>
              <div>Criado em: {memory.created_at}</div>
            </CardContent>
          </Card>

          <Card className="border-zinc-800 bg-zinc-900">
            <CardHeader className="space-y-2">
              <CardTitle className="text-zinc-100">Últimos acessos</CardTitle>
              <AccessLogFilter
                value={channel}
                meta={accessLogMeta}
                onChange={setChannel}
              />
              <AccessLogPartialNotice meta={accessLogMeta} />
            </CardHeader>
            <CardContent className="text-sm text-zinc-400">
              {accessLogs.length === 0 ? (
                <span className="text-zinc-600">Nenhum acesso registrado</span>
              ) : (
                <ul className="space-y-3">
                  {accessLogs.map((log) => (
                    <li key={log.id}>
                      <AccessLogEntryView entry={log} variant="compact" />
                    </li>
                  ))}
                </ul>
              )}
            </CardContent>
          </Card>
        </div>
      )}
    </div>
  );
}
