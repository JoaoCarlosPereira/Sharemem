"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Trophy } from "lucide-react";
import { PageHeader } from "@/components/shared/PageHeader";
import { StatCard } from "@/components/admin/StatCard";
import { UsersSubNav } from "@/components/admin/UsersSubNav";
import { ContributorFilters } from "@/components/admin/ContributorFilters";
import { TopContributorsChart } from "@/components/admin/TopContributorsChart";
import { ContributorsRankingTable } from "@/components/admin/ContributorsRankingTable";
import { useUserAnalyticsApi } from "@/hooks/useUserAnalyticsApi";
import { useAdminApi } from "@/hooks/useAdminApi";
import type {
  ContributorFilters as ContributorFiltersState,
  GroupAnalytics,
  ProjectSize,
  TopContributorsResponse,
} from "@/types/admin";

function errorMessage(err: unknown, fallback: string): string {
  const e = err as { response?: { data?: { detail?: string } }; message?: string };
  return e?.response?.data?.detail || e?.message || fallback;
}

const DEFAULT_FILTERS: ContributorFiltersState = {
  metric: "total",
  period: "7d",
};

export default function TopContributorsPage() {
  const { fetchTopContributors, fetchGroupsAnalytics } = useUserAnalyticsApi();
  const { fetchProjectSizes } = useAdminApi({ poll: false });

  const [filters, setFilters] = useState<ContributorFiltersState>(DEFAULT_FILTERS);
  const [data, setData] = useState<TopContributorsResponse | null>(null);
  const [groups, setGroups] = useState<GroupAnalytics[]>([]);
  const [projects, setProjects] = useState<ProjectSize[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    void (async () => {
      try {
        const [groupsData, projectData] = await Promise.all([
          fetchGroupsAnalytics(),
          fetchProjectSizes(),
        ]);
        setGroups(groupsData);
        setProjects(projectData.projects);
      } catch {
        // Filters still work with empty option lists.
      }
    })();
  }, [fetchGroupsAnalytics, fetchProjectSizes]);

  const reload = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await fetchTopContributors(filters));
    } catch (err) {
      setError(errorMessage(err, "Falha ao carregar ranking de contribuidores"));
    } finally {
      setLoading(false);
    }
  }, [fetchTopContributors, filters]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const stats = useMemo(() => {
    const items = data?.items ?? [];
    const isScore = filters.metric === "total";
    const topValue = isScore ? (items[0]?.value ?? 0).toFixed(1) : (items[0]?.value ?? 0);
    const totalTop10 = items.reduce(
      (sum, item) => sum + (isScore ? item.writes + item.reads : item.value),
      0,
    );
    const activeUsers = items.length;
    const distinctProjects = items.reduce(
      (max, item) => Math.max(max, item.distinct_projects),
      0,
    );
    return { topValue, totalTop10, activeUsers, distinctProjects };
  }, [data, filters.metric]);

  return (
    <div className="max-w-[1600px] space-y-6 pb-12">
      <PageHeader
        className="mb-2"
        icon={Trophy}
        title="Top Contribuidores"
        description="Ranking dos usuários que mais contribuem com memórias — por escrita, consulta ou atividade geral"
      />

      <UsersSubNav />

      {error && (
        <div
          role="alert"
          className="rounded-md border border-red-800 bg-red-950/40 px-3 py-2 text-sm text-red-300"
        >
          {error}
        </div>
      )}

      <ContributorFilters
        filters={filters}
        groups={groups}
        projects={projects}
        onChange={setFilters}
      />

      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <StatCard title="Líder do ranking" value={stats.topValue} accent="violet" />
        <StatCard title="Soma do top 10" value={stats.totalTop10} accent="blue" />
        <StatCard title="Usuários no ranking" value={stats.activeUsers} accent="cyan" />
        <StatCard
          title="Projetos (máx. no top)"
          value={stats.distinctProjects}
          accent="emerald"
        />
      </div>

      <TopContributorsChart
        items={data?.items ?? []}
        metric={filters.metric}
        loading={loading}
      />

      <ContributorsRankingTable items={data?.items ?? []} metric={filters.metric} />
    </div>
  );
}
