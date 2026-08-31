import { ArrowRight } from "lucide-react";
import Categories from "@/components/shared/categories";
import Link from "next/link";
import { AttributionBadge } from "@/components/shared/attribution-badge";
import { formatDateTime } from "@/lib/i18n/pt-BR";

interface MemoryCardProps {
  id: string;
  content: string;
  created_at: string | number;
  metadata?: Record<string, unknown>;
  categories?: string[];
  access_count?: number;
  accessed_by_client?: string | null;
  attribution_prefix?: string;
  app_name: string;
  created_by_hostname?: string | null;
  created_by_client?: string | null;
  created_by_display_name?: string | null;
  created_by_avatar_url?: string | null;
  state: string;
}

export function MemoryCard({
  id,
  content,
  created_at,
  metadata,
  categories,
  access_count,
  accessed_by_client,
  attribution_prefix = "Criada por:",
  app_name,
  created_by_hostname,
  created_by_client,
  created_by_display_name,
  created_by_avatar_url,
  state,
}: MemoryCardProps) {
  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-900 overflow-hidden">
      <div className="p-4">
        <div className="border-l-2 border-primary pl-4 mb-4">
          <p
            className={`${state !== "active" ? "text-zinc-400" : "text-white"}`}
          >
            {content}
          </p>
        </div>

        {metadata && Object.keys(metadata).length > 0 && (
          <div className="mb-4">
            <p className="text-xs text-zinc-500 uppercase mb-2">METADADOS</p>
            <div className="bg-zinc-800 rounded p-3 text-zinc-400">
              <pre className="whitespace-pre-wrap">
                {JSON.stringify(metadata, null, 2)}
              </pre>
            </div>
          </div>
        )}

        <div className="mb-2">
          <Categories
            categories={categories as any}
            isPaused={state !== "active"}
          />
        </div>

        <div className="flex justify-between items-center">
          <div className="flex items-center gap-2">
            <span className="text-zinc-400 text-sm">
              {access_count ? (
                <span className="relative top-1">
                  Acessada {access_count} vezes
                </span>
              ) : (
                formatDateTime(created_at)
              )}
            </span>

            {state !== "active" && (
              <span className="inline-block px-3 border border-yellow-600 text-yellow-600 font-semibold text-xs rounded-full bg-yellow-400/10 backdrop-blur-sm">
                {state === "paused" ? "Pausada" : "Arquivada"}
              </span>
            )}
          </div>

          <div className="flex items-center gap-2">
            <Link
              href={`/memory/${id}`}
              className="hover:cursor-pointer bg-zinc-800 hover:bg-zinc-700 flex items-center px-3 py-1 text-sm rounded-lg text-white p-0 hover:text-white"
            >
              Ver Detalhes
              <ArrowRight className="ml-2 h-4 w-4" />
            </Link>
            <AttributionBadge
              appName={attribution_prefix === "Acessada por:" ? "openmemory" : app_name}
              clientName={attribution_prefix === "Acessada por:" ? accessed_by_client : created_by_client}
              hostname={attribution_prefix === "Acessada por:" ? null : created_by_hostname}
              displayName={attribution_prefix === "Acessada por:" ? null : created_by_display_name}
              avatarUrl={attribution_prefix === "Acessada por:" ? null : created_by_avatar_url}
              metadata={attribution_prefix === "Acessada por:" ? null : metadata}
              prefix={attribution_prefix}
            />
          </div>
        </div>
      </div>
    </div>
  );
}
