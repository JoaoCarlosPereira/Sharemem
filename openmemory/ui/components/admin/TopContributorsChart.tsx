"use client";

import { Bar, BarChart, CartesianGrid, XAxis, YAxis } from "recharts";
import { Skeleton } from "@/components/ui/skeleton";
import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart";
import { resolveAttribution } from "@/lib/attribution";
import type { ContributorMetric, TopContributor } from "@/types/admin";

const AVATAR_SIZE = 28;

const METRIC_CONFIG: Record<
  ContributorMetric,
  { label: string; description: string; color: string; accent: string }
> = {
  writes: {
    label: "Escritas",
    description: "Ranking por escritas no período selecionado",
    color: "hsl(152 76% 45%)",
    accent: "text-emerald-300",
  },
  reads: {
    label: "Consultas",
    description: "Ranking por consultas no período selecionado",
    color: "hsl(189 94% 43%)",
    accent: "text-cyan-300",
  },
  total: {
    label: "Score",
    description: "Score de 0 a 100 no período selecionado: 70% escritas e 30% consultas",
    color: "hsl(258 90% 66%)",
    accent: "text-violet-300",
  },
};

interface TopContributorsChartProps {
  items: TopContributor[];
  metric: ContributorMetric;
  loading?: boolean;
}

type ContributorTickProps = {
  x?: number;
  y?: number;
  index?: number;
};

function createContributorAvatarTick(items: TopContributor[]) {
  return function ContributorAvatarTick({
    x = 0,
    y = 0,
    index = 0,
  }: ContributorTickProps) {
    const item = items[index];
    if (!item) {
      return null;
    }

    const attribution = resolveAttribution({
      hostname: item.user_id,
      displayName: item.display_name,
      avatarUrl: item.avatar_url,
    });
    const imageUrl = attribution.avatarUrl ?? attribution.iconImage;
    const hint = item.display_name || item.user_id;
    const half = AVATAR_SIZE / 2;
    const clipId = `contributor-avatar-${item.rank}`;

    return (
      <g transform={`translate(${x},${y})`}>
        <title>{hint}</title>
        {imageUrl ? (
          <>
            <defs>
              <clipPath id={clipId}>
                <circle cx={0} cy={half + 2} r={half} />
              </clipPath>
            </defs>
            <image
              href={imageUrl}
              x={-half}
              y={2}
              width={AVATAR_SIZE}
              height={AVATAR_SIZE}
              clipPath={`url(#${clipId})`}
              preserveAspectRatio="xMidYMid slice"
            />
          </>
        ) : (
          <circle cx={0} cy={half + 2} r={half} fill="#52525b" />
        )}
      </g>
    );
  };
}

export function TopContributorsChart({
  items,
  metric,
  loading = false,
}: TopContributorsChartProps) {
  const metricConfig = METRIC_CONFIG[metric];
  const chartConfig = {
    value: {
      label: metricConfig.label,
      color: metricConfig.color,
    },
  } satisfies ChartConfig;

  if (loading) {
    return <Skeleton className="h-[420px] w-full rounded-xl" />;
  }

  if (items.length === 0) {
    return (
      <div className="flex h-[420px] flex-col items-center justify-center rounded-xl border border-dashed border-zinc-800 bg-zinc-950/40 px-6 text-center">
        <p className="text-lg font-medium text-zinc-200">Nenhuma contribuição no período</p>
        <p className="mt-2 max-w-md text-sm text-zinc-500">
          Ajuste os filtros ou aguarde novas escritas e consultas na memória compartilhada.
        </p>
      </div>
    );
  }

  const rows = items;
  const AvatarTick = createContributorAvatarTick(rows);

  return (
    <div className="rounded-xl border border-zinc-800 bg-zinc-950/30 p-4">
      <div className="mb-4 flex items-end justify-between gap-4">
        <div>
          <h2 className="text-lg font-semibold text-white">Top 10 contribuidores</h2>
          <p className="text-sm text-zinc-500">
            {metricConfig.description}
          </p>
        </div>
        <span className={`text-sm font-medium ${metricConfig.accent}`}>
          {metricConfig.label}
        </span>
      </div>

      <ChartContainer config={chartConfig} className="aspect-auto h-[360px] w-full">
        <BarChart data={rows} margin={{ top: 12, right: 12, left: 0, bottom: 4 }}>
          <CartesianGrid vertical={false} strokeDasharray="3 3" />
          <XAxis
            dataKey="user_id"
            tickLine={false}
            axisLine={false}
            interval={0}
            height={44}
            tick={(props) => <AvatarTick {...props} />}
          />
          <YAxis allowDecimals={false} tickLine={false} axisLine={false} width={40} />
          <ChartTooltip
            cursor={{ fill: "rgba(255,255,255,0.04)" }}
            content={
              <ChartTooltipContent
                labelFormatter={(_, payload) => {
                  const item = payload?.[0]?.payload as TopContributor | undefined;
                  if (!item) return "";
                  return item.display_name || item.user_id;
                }}
                formatter={(value, _name, item) => {
                  const row = item.payload as TopContributor;
                  return (
                    <div className="space-y-1">
                      <div className="font-medium">{value}</div>
                      <div className="text-xs text-zinc-500">
                        {row.group_name ? `${row.group_name} · ` : ""}
                        {row.writes} escritas · {row.reads} consultas
                      </div>
                    </div>
                  );
                }}
              />
            }
          />
          <Bar
            dataKey="value"
            name="value"
            fill="var(--color-value)"
            radius={[6, 6, 0, 0]}
          />
        </BarChart>
      </ChartContainer>
    </div>
  );
}

export default TopContributorsChart;
