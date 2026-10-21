"use client";

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import type {
  ContributorFilters as ContributorFiltersState,
  ContributorMetric,
  ContributorPeriod,
  GroupAnalytics,
  ProjectSize,
} from "@/types/admin";

const ALL = "all";

const PERIODS: { value: ContributorPeriod; label: string }[] = [
  { value: "24h", label: "24 horas" },
  { value: "7d", label: "7 dias" },
  { value: "30d", label: "30 dias" },
  { value: "all", label: "Sempre" },
];

interface ContributorFiltersProps {
  filters: ContributorFiltersState;
  groups: GroupAnalytics[];
  projects: ProjectSize[];
  onChange: (filters: ContributorFiltersState) => void;
}

export function ContributorFilters({
  filters,
  groups,
  projects,
  onChange,
}: ContributorFiltersProps) {
  const update = (patch: Partial<ContributorFiltersState>) => {
    onChange({ ...filters, ...patch });
  };

  return (
    <div className="mb-6 space-y-4 rounded-xl border border-zinc-800 bg-zinc-900/40 p-4">
      <div className="space-y-2">
        <p className="text-xs font-semibold uppercase tracking-widest text-zinc-500">
          Métrica
        </p>
        <ToggleGroup
          type="single"
          value={filters.metric}
          onValueChange={(value) => {
            if (value) update({ metric: value as ContributorMetric });
          }}
          className="flex flex-wrap justify-start gap-2"
        >
          <ToggleGroupItem
            value="writes"
            aria-label="Escritas"
            className="rounded-lg border border-zinc-800 bg-zinc-950 px-4 data-[state=on]:border-emerald-700 data-[state=on]:bg-emerald-950/40 data-[state=on]:text-emerald-200"
          >
            Escritas
          </ToggleGroupItem>
          <ToggleGroupItem
            value="reads"
            aria-label="Consultas"
            className="rounded-lg border border-zinc-800 bg-zinc-950 px-4 data-[state=on]:border-cyan-700 data-[state=on]:bg-cyan-950/40 data-[state=on]:text-cyan-200"
          >
            Consultas
          </ToggleGroupItem>
          <ToggleGroupItem
            value="total"
            aria-label="Geral"
            className="rounded-lg border border-zinc-800 bg-zinc-950 px-4 data-[state=on]:border-violet-700 data-[state=on]:bg-violet-950/40 data-[state=on]:text-violet-200"
          >
            Geral
          </ToggleGroupItem>
        </ToggleGroup>
        {filters.metric === "total" && (
          <p className="text-xs text-zinc-500">
            Score de 0 a 100: 70% escritas e 30% consultas, cada uma proporcional ao maior valor
            do recorte filtrado.
          </p>
        )}
      </div>

      <div className="grid gap-3 md:grid-cols-3">
        <label className="flex flex-col gap-1 text-xs text-zinc-400">
          Período
          <Select
            value={filters.period}
            onValueChange={(value) => update({ period: value as ContributorPeriod })}
          >
            <SelectTrigger className="border-zinc-800 bg-zinc-950" aria-label="Período">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {PERIODS.map((period) => (
                <SelectItem key={period.value} value={period.value}>
                  {period.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </label>

        <label className="flex flex-col gap-1 text-xs text-zinc-400">
          Grupo
          <Select
            value={filters.groupId ?? ALL}
            onValueChange={(value) =>
              update({ groupId: value === ALL ? undefined : value })
            }
          >
            <SelectTrigger className="border-zinc-800 bg-zinc-950" aria-label="Grupo">
              <SelectValue placeholder="Todos os grupos" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL}>Todos os grupos</SelectItem>
              {groups.map((group) => (
                <SelectItem key={group.id} value={group.id}>
                  {group.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </label>

        <label className="flex flex-col gap-1 text-xs text-zinc-400">
          Projeto
          <Select
            value={filters.project ?? ALL}
            onValueChange={(value) =>
              update({ project: value === ALL ? undefined : value })
            }
          >
            <SelectTrigger className="border-zinc-800 bg-zinc-950" aria-label="Projeto">
              <SelectValue placeholder="Todos os projetos" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL}>Todos os projetos</SelectItem>
              {projects.map((project) => (
                <SelectItem key={project.name} value={project.name}>
                  {project.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </label>
      </div>
    </div>
  );
}

export default ContributorFilters;
