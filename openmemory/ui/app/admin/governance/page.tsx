"use client";

import { useCallback, useEffect, useState } from "react";
import axios from "axios";
import { toast } from "sonner";
import { useSelector } from "react-redux";
import { formatDateTimeFull } from "@/lib/datetime";
import { RootState } from "@/store/store";
import { useQueuesApi } from "@/hooks/useQueuesApi";
import { useAdminApi } from "@/hooks/useAdminApi";
import { QueueTable, QueueColumn } from "@/components/admin/QueueTable";
import { JobStatusBadge } from "@/components/admin/JobStatusBadge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Input } from "@/components/ui/input";
import { Checkbox } from "@/components/ui/checkbox";
import { Switch } from "@/components/ui/switch";
import { GovernanceJob, ProjectSize } from "@/types/admin";
import { getApiUrl } from "@/lib/api-url";
import { PageHeader } from "@/components/shared/PageHeader";
import { Shield } from "lucide-react";

type ProjectMergePreviewGroup = {
  canonical: string;
  aliases: string[];
  confidence: number;
  reason: string;
  memory_counts: Record<string, number>;
};

type ProjectMergePreviewResponse = {
  groups: ProjectMergePreviewGroup[];
  count: number;
};

type MergeProposalStatus =
  | "pending"
  | "approved"
  | "rejected"
  | "applied"
  | "failed";

type MergeProposal = {
  id: string;
  canonical: string;
  aliases: string[];
  confidence: number;
  reason: string;
  status: MergeProposalStatus;
  origin?: string | null;
  memory_counts?: Record<string, number> | null;
  decided_by?: string | null;
  last_error?: string | null;
  created_at?: string | null;
};

const PROPOSAL_STATUS_LABEL: Record<MergeProposalStatus, string> = {
  pending: "Pendente",
  approved: "Aprovada (na fila)",
  rejected: "Rejeitada",
  applied: "Aplicada",
  failed: "Falhou",
};

type ScheduleConfig = {
  schedule_timezone: string;
  schedule_weekdays: number[];
  schedule_start_time: string;
  schedule_end_time: string;
};

const WEEKDAYS: { value: number; label: string }[] = [
  { value: 0, label: "Seg" },
  { value: 1, label: "Ter" },
  { value: 2, label: "Qua" },
  { value: 3, label: "Qui" },
  { value: 4, label: "Sex" },
  { value: 5, label: "Sáb" },
  { value: 6, label: "Dom" },
];

const TIMEZONES = [
  "America/Sao_Paulo",
  "America/Manaus",
  "America/Fortaleza",
  "UTC",
];


const PROCESS_DEFS: { type: string; label: string; description: string }[] = [
  {
    type: "dedup",
    label: "Deduplicação",
    description: "Isola memórias duplicadas por similaridade em quarentena",
  },
  {
    type: "ttl_prune",
    label: "TTL Prune",
    description: "Quarentena de memórias acima da idade/inatividade máxima",
  },
  {
    type: "consolidate",
    label: "Consolidação",
    description: "Mescla memórias redundantes e resolve contradições via LLM",
  },
  {
    type: "purge",
    label: "Purga",
    description: "Exclusão definitiva de memórias em quarentena (destrutivo)",
  },
  {
    type: "quality_eval",
    label: "Avaliação de qualidade",
    description: "Amostra buscas e calcula índices de qualidade (proxy/LLM)",
  },
  {
    type: "enforce_quota",
    label: "Cota de memórias",
    description: "Aplica o teto de memórias por projeto quando configurado",
  },
  {
    type: "cold_tier",
    label: "Cold tier",
    description: "Arquivamento de projetos inativos para o cold tier",
  },
  {
    type: "merge_projects",
    label: "Unificação de projetos",
    description:
      "Propõe unificar projetos duplicados (LLM); aplica só após aprovação de admin",
  },
];

const JOB_TYPES: { type: string; label: string; destructive?: boolean }[] = [
  { type: "dedup", label: "Deduplicar" },
  { type: "ttl_prune", label: "TTL Prune" },
  { type: "consolidate", label: "Consolidar" },
  { type: "purge", label: "Purgar", destructive: true },
];

export default function GovernancePage() {
  const { fetchGovernanceJobs } = useQueuesApi();
  const { fetchProjectSizes } = useAdminApi({ poll: false });
  const governanceQueue = useSelector(
    (s: RootState) => s.queues.governanceQueue,
  );

  const [projects, setProjects] = useState<ProjectSize[]>([]);
  const [policies, setPolicies] = useState<Record<string, unknown> | null>(
    null,
  );
  const [dialogJob, setDialogJob] = useState<{
    type: string;
    label: string;
    destructive?: boolean;
  } | null>(null);
  const [selectedProject, setSelectedProject] = useState<string>("");
  const [dispatching, setDispatching] = useState(false);
  const [mergePreview, setMergePreview] = useState<ProjectMergePreviewGroup[] | null>(
    null,
  );
  const [mergeLoading, setMergeLoading] = useState(false);
  const [mergeDialogOpen, setMergeDialogOpen] = useState(false);
  const [schedule, setSchedule] = useState<ScheduleConfig | null>(null);
  const [scheduleSaving, setScheduleSaving] = useState(false);
  const [processes, setProcesses] = useState<Record<string, boolean> | null>(
    null,
  );
  const [processSaving, setProcessSaving] = useState<string | null>(null);
  const [proposals, setProposals] = useState<MergeProposal[] | null>(null);
  const [proposalFilter, setProposalFilter] = useState<string>("pending");
  const [proposalBusy, setProposalBusy] = useState<string | null>(null);

  const loadProposals = useCallback(async () => {
    try {
      const res = await axios.get<{ items: MergeProposal[]; count: number }>(
        `${getApiUrl()}/admin/governance/projects/merge-proposals`,
        {
          params: {
            status: proposalFilter === "all" ? undefined : proposalFilter,
            limit: 100,
          },
        },
      );
      setProposals(Array.isArray(res.data?.items) ? res.data.items : []);
    } catch {
      setProposals([]);
      toast.error("Falha ao carregar propostas de unificação");
    }
  }, [proposalFilter]);

  useEffect(() => {
    loadProposals();
  }, [loadProposals]);

  const handleProposalDecision = useCallback(
    async (proposal: MergeProposal, action: "approve" | "reject") => {
      setProposalBusy(proposal.id);
      try {
        await axios.post(
          `${getApiUrl()}/admin/governance/projects/merge-proposals/${proposal.id}/${action}`,
          {},
        );
        toast.success(
          action === "approve"
            ? `Unificação em '${proposal.canonical}' aprovada e enfileirada`
            : "Proposta rejeitada",
        );
        await Promise.all([loadProposals(), fetchGovernanceJobs()]);
      } catch (err) {
        const detail = axios.isAxiosError(err)
          ? (err.response?.data as { detail?: string } | undefined)?.detail
          : undefined;
        toast.error(detail ?? "Falha ao registrar a decisão");
        await loadProposals();
      } finally {
        setProposalBusy(null);
      }
    },
    [loadProposals, fetchGovernanceJobs],
  );

  useEffect(() => {
    fetchProjectSizes()
      .then((res) => setProjects(res.projects))
      .catch(() => {});
    axios
      .get(`${getApiUrl()}/admin/governance/policies`)
      .then((res) => setPolicies(res.data))
      .catch(() => {});
    axios
      .get<ScheduleConfig>(`${getApiUrl()}/admin/governance/schedule`)
      .then((res) => setSchedule(res.data))
      .catch(() => {});
    axios
      .get<{ processes_enabled: Record<string, boolean> }>(
        `${getApiUrl()}/admin/governance/processes`,
      )
      .then((res) => setProcesses(res.data.processes_enabled))
      .catch(() => {});
  }, [fetchProjectSizes]);

  const handleDispatch = useCallback(async () => {
    if (!dialogJob) return;
    setDispatching(true);
    try {
      await axios.post(`${getApiUrl()}/admin/governance/jobs/${dialogJob.type}`, {
        project: selectedProject || null,
      });
      toast.success(`Job ${dialogJob.label} enfileirado`);
      setDialogJob(null);
      setSelectedProject("");
      await fetchGovernanceJobs();
    } catch {
      toast.error("Falha ao disparar o job");
    } finally {
      setDispatching(false);
    }
  }, [dialogJob, selectedProject, fetchGovernanceJobs]);

  const handleAnalyzeDuplicates = useCallback(async () => {
    setMergeLoading(true);
    setMergeDialogOpen(true);
    setMergePreview(null);
    try {
      const res = await axios.get<ProjectMergePreviewResponse>(
        `${getApiUrl()}/admin/governance/projects/merge-preview`,
      );
      setMergePreview(res.data.groups);
      if (res.data.count === 0) {
        toast.info("Nenhum projeto duplicado detectado pelo LLM.");
      }
    } catch {
      toast.error("Falha ao analisar projetos duplicados");
      setMergeDialogOpen(false);
    } finally {
      setMergeLoading(false);
    }
  }, []);

  const handleMergeProjects = useCallback(async () => {
    setMergeLoading(true);
    try {
      await axios.post(`${getApiUrl()}/admin/governance/projects/merge`, {
        dry_run: false,
      });
      toast.success(
        "Propostas de unificação enfileiradas — aprovação de admin necessária para aplicar",
      );
      setMergeDialogOpen(false);
      setMergePreview(null);
      await Promise.all([fetchGovernanceJobs(), loadProposals()]);
    } catch {
      toast.error("Falha ao enfileirar propostas de unificação");
    } finally {
      setMergeLoading(false);
    }
  }, [fetchGovernanceJobs, loadProposals]);

  const toggleWeekday = useCallback((day: number, checked: boolean) => {
    setSchedule((prev) => {
      if (!prev) return prev;
      const set = new Set(prev.schedule_weekdays);
      if (checked) set.add(day);
      else set.delete(day);
      return {
        ...prev,
        schedule_weekdays: Array.from(set).sort((a, b) => a - b),
      };
    });
  }, []);

  const handleSaveSchedule = useCallback(async () => {
    if (!schedule || schedule.schedule_weekdays.length === 0) {
      toast.error("Selecione pelo menos um dia da semana");
      return;
    }
    setScheduleSaving(true);
    try {
      const res = await axios.put<ScheduleConfig>(
        `${getApiUrl()}/admin/governance/schedule`,
        schedule,
      );
      setSchedule(res.data);
      toast.success("Agendamento de governança salvo");
    } catch {
      toast.error("Falha ao salvar agendamento");
    } finally {
      setScheduleSaving(false);
    }
  }, [schedule]);

  const isProcessEnabled = useCallback(
    (type: string) => processes?.[type] ?? true,
    [processes],
  );

  const handleToggleProcess = useCallback(
    async (type: string, enabled: boolean) => {
      if (!processes) return;
      setProcessSaving(type);
      try {
        const next = { ...processes, [type]: enabled };
        const res = await axios.put(
          `${getApiUrl()}/admin/governance/processes`,
          { processes_enabled: next },
        );
        setProcesses(res.data.processes_enabled);
        toast.success(
          enabled ? "Processo habilitado" : "Processo desabilitado",
        );
      } catch {
        toast.error("Falha ao atualizar o estado do processo");
        axios
          .get<{ processes_enabled: Record<string, boolean> }>(
            `${getApiUrl()}/admin/governance/processes`,
          )
          .then((res) => setProcesses(res.data.processes_enabled))
          .catch(() => {});
      } finally {
        setProcessSaving(null);
      }
    },
    [processes],
  );

  const columns: QueueColumn<GovernanceJob>[] = [
    { key: "job_type", header: "Tipo", render: (r) => r.job_type },
    {
      key: "project",
      header: "Projeto",
      render: (r) => r.project ?? <span className="text-zinc-600">global</span>,
    },
    {
      key: "status",
      header: "Status",
      render: (r) => <JobStatusBadge status={r.status} />,
    },
    {
      key: "created_at",
      header: "Criado em",
      render: (r) => formatDateTimeFull(r.created_at),
    },
  ];

  return (
    <div>
      <PageHeader
        className="mb-4"
        icon={Shield}
        title="Governança"
        description="Deduplicação, consolidação, TTL e merge de projetos"
      />

      <section className="mb-6">
        <h2 className="mb-2 text-sm font-medium text-zinc-400">
          Disparo manual
        </h2>
        <div className="flex flex-wrap gap-2">
          {JOB_TYPES.map((jt) => (
            <Button
              key={jt.type}
              variant={jt.destructive ? "destructive" : "outline"}
              size="sm"
              disabled={!isProcessEnabled(jt.type)}
              onClick={() => setDialogJob(jt)}
            >
              {jt.label}
            </Button>
          ))}
        </div>
      </section>

      <section className="mb-6">
        <h2 className="mb-2 text-sm font-medium text-zinc-400">
          Unificação de projetos (LLM)
        </h2>
        <p className="mb-3 max-w-2xl text-sm text-zinc-500">
          Detecta projetos MCP que representam o mesmo workspace (ex.:{" "}
          <code className="text-zinc-400">sysmovs</code>,{" "}
          <code className="text-zinc-400">dsv-delphi-sysmovs</code>) e gera
          propostas de unificação para um nome canônico. Nada é movido
          automaticamente: cada proposta precisa ser aprovada por um admin.
          Projetos <code className="text-zinc-400">default</code>,{" "}
          <code className="text-zinc-400">tarefa-*</code> e numéricos nunca
          são unificados.
        </p>
        <div className="flex flex-wrap gap-2">
          <Button
            variant="outline"
            size="sm"
            disabled={mergeLoading || !isProcessEnabled("merge_projects")}
            onClick={handleAnalyzeDuplicates}
          >
            Analisar Duplicatas
          </Button>
        </div>
      </section>

      <section className="mb-6">
        <div className="mb-2 flex items-center justify-between gap-4">
          <h2 className="text-sm font-medium text-zinc-400">
            Propostas de unificação
          </h2>
          <Select value={proposalFilter} onValueChange={setProposalFilter}>
            <SelectTrigger
              className="h-8 w-48"
              aria-label="Filtrar propostas por status"
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">Todas</SelectItem>
              {(Object.keys(PROPOSAL_STATUS_LABEL) as MergeProposalStatus[]).map(
                (s) => (
                  <SelectItem key={s} value={s}>
                    {PROPOSAL_STATUS_LABEL[s]}
                  </SelectItem>
                ),
              )}
            </SelectContent>
          </Select>
        </div>
        <p className="mb-3 max-w-2xl text-sm text-zinc-500">
          Aprovar enfileira a aplicação (requer o processo habilitado).
          Propostas que falharam podem ser aprovadas de novo para repetir.
        </p>
        {proposals === null ? (
          <p className="text-sm text-zinc-500">Carregando propostas…</p>
        ) : proposals.length === 0 ? (
          <p className="text-sm text-zinc-500">Nenhuma proposta.</p>
        ) : (
          <div className="grid max-w-4xl gap-2">
            {proposals.map((p) => {
              const canApprove =
                (p.status === "pending" || p.status === "failed") &&
                isProcessEnabled("merge_projects");
              const canReject =
                p.status === "pending" ||
                p.status === "approved" ||
                p.status === "failed";
              return (
                <div
                  key={p.id}
                  className="flex items-start justify-between gap-4 rounded-md border border-zinc-800 bg-zinc-900/50 px-4 py-2.5 text-sm"
                >
                  <div className="min-w-0">
                    <p className="text-zinc-200">
                      {p.aliases.join(", ")} →{" "}
                      <span className="font-medium">{p.canonical}</span>
                    </p>
                    <p className="text-xs text-zinc-500">
                      {PROPOSAL_STATUS_LABEL[p.status] ?? p.status} ·
                      confiança {(p.confidence * 100).toFixed(0)}%
                      {p.origin ? ` · ${p.origin}` : ""}
                      {p.created_at
                        ? ` · ${formatDateTimeFull(p.created_at)}`
                        : ""}
                    </p>
                    {p.reason && (
                      <p className="truncate text-xs text-zinc-500">
                        {p.reason}
                      </p>
                    )}
                    {p.last_error && (
                      <p className="text-xs text-red-400">{p.last_error}</p>
                    )}
                  </div>
                  <div className="flex shrink-0 gap-2">
                    {canApprove && (
                      <Button
                        size="sm"
                        disabled={proposalBusy === p.id}
                        onClick={() => handleProposalDecision(p, "approve")}
                      >
                        {p.status === "failed" ? "Tentar de novo" : "Aprovar"}
                      </Button>
                    )}
                    {canReject && (
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={proposalBusy === p.id}
                        onClick={() => handleProposalDecision(p, "reject")}
                      >
                        Rejeitar
                      </Button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </section>

      <section className="mb-6">
        <h2 className="mb-2 text-sm font-medium text-zinc-400">
          Jobs de governança
        </h2>
        <QueueTable
          columns={columns}
          data={governanceQueue?.items ?? []}
          page={governanceQueue?.page ?? 1}
          pages={governanceQueue?.pages ?? 0}
          onPageChange={() => {}}
          emptyMessage="Nenhum job de governança"
        />
      </section>

      <section className="mb-6">
        <h2 className="mb-2 text-sm font-medium text-zinc-400">
          Processos de governança
        </h2>
        <p className="mb-3 max-w-2xl text-sm text-zinc-500">
          Habilite ou desabilite cada processo individualmente. Desabilitar um
          processo impede o agendamento, o disparo manual e a execução dos jobs
          já na fila, que ficam pausados até a reativação.
        </p>
        {processes ? (
          <div className="grid max-w-3xl gap-2">
            {PROCESS_DEFS.map((proc) => (
              <div
                key={proc.type}
                className="flex items-center justify-between gap-4 rounded-md border border-zinc-800 bg-zinc-900/50 px-4 py-2.5"
              >
                <div className="min-w-0">
                  <p className="text-sm text-zinc-200">{proc.label}</p>
                  <p className="truncate text-xs text-zinc-500">
                    {proc.description}
                  </p>
                </div>
                <Switch
                  checked={processes[proc.type] ?? true}
                  disabled={processSaving === proc.type}
                  onCheckedChange={(checked) =>
                    handleToggleProcess(proc.type, checked === true)
                  }
                  aria-label={`Habilitar ${proc.label}`}
                />
              </div>
            ))}
          </div>
        ) : (
          <p className="text-sm text-zinc-500">
            Carregando processos…
          </p>
        )}
      </section>

      <section className="mb-6">
        <h2 className="mb-2 text-sm font-medium text-zinc-400">
          Agendamento automático
        </h2>
        <p className="mb-3 max-w-2xl text-sm text-zinc-500">
          Jobs agendados (dedup, TTL, consolidação, etc.) só rodam nesta janela.
          Disparos manuais ignoram o agendamento.
        </p>
        {schedule ? (
          <div className="max-w-xl space-y-4 rounded-md border border-zinc-800 bg-zinc-900/50 p-4">
            <div>
              <label className="mb-2 block text-sm text-zinc-400">
                Dias da semana
              </label>
              <div className="flex flex-wrap gap-3">
                {WEEKDAYS.map((day) => (
                  <label
                    key={day.value}
                    className="flex cursor-pointer items-center gap-2 text-sm text-zinc-300"
                  >
                    <Checkbox
                      checked={(schedule.schedule_weekdays ?? []).includes(
                        day.value,
                      )}
                      onCheckedChange={(checked) =>
                        toggleWeekday(day.value, checked === true)
                      }
                      aria-label={day.label}
                    />
                    {day.label}
                  </label>
                ))}
              </div>
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="mb-1 block text-sm text-zinc-400">
                  Início
                </label>
                <Input
                  type="time"
                  value={schedule.schedule_start_time}
                  onChange={(e) =>
                    setSchedule((prev) =>
                      prev
                        ? { ...prev, schedule_start_time: e.target.value }
                        : prev,
                    )
                  }
                />
              </div>
              <div>
                <label className="mb-1 block text-sm text-zinc-400">Fim</label>
                <Input
                  type="time"
                  value={schedule.schedule_end_time}
                  onChange={(e) =>
                    setSchedule((prev) =>
                      prev
                        ? { ...prev, schedule_end_time: e.target.value }
                        : prev,
                    )
                  }
                />
              </div>
            </div>
            <div>
              <label className="mb-1 block text-sm text-zinc-400">
                Fuso horário
              </label>
              <Select
                value={schedule.schedule_timezone}
                onValueChange={(value) =>
                  setSchedule((prev) =>
                    prev ? { ...prev, schedule_timezone: value } : prev,
                  )
                }
              >
                <SelectTrigger aria-label="Fuso horário">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {TIMEZONES.map((tz) => (
                    <SelectItem key={tz} value={tz}>
                      {tz}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <Button
              size="sm"
              disabled={scheduleSaving}
              onClick={handleSaveSchedule}
            >
              {scheduleSaving ? "Salvando…" : "Salvar agendamento"}
            </Button>
          </div>
        ) : (
          <p className="text-sm text-zinc-500">Carregando agendamento…</p>
        )}
      </section>

      <section>
        <h2 className="mb-2 text-sm font-medium text-zinc-400">
          Políticas ativas (somente leitura)
        </h2>
        <pre className="max-h-64 overflow-auto rounded-md border border-zinc-800 bg-zinc-900 p-3 text-xs text-zinc-300">
          {policies ? JSON.stringify(policies, null, 2) : "Carregando…"}
        </pre>
      </section>

      <Dialog
        open={dialogJob !== null}
        onOpenChange={(o) => {
          if (!o) {
            setDialogJob(null);
            setSelectedProject("");
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Disparar {dialogJob?.label}</DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            {dialogJob?.destructive && (
              <p className="text-sm font-semibold text-red-500">
                Esta ação é DESTRUTIVA e não pode ser desfeita.
              </p>
            )}
            <label className="block text-sm text-zinc-400">
              Projeto-alvo
            </label>
            <Select
              value={selectedProject || "__all__"}
              onValueChange={(v) =>
                setSelectedProject(v === "__all__" ? "" : v)
              }
            >
              <SelectTrigger aria-label="Selecionar projeto">
                <SelectValue placeholder="Todos os projetos" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="__all__">Todos os projetos</SelectItem>
                {projects.map((p) => (
                  <SelectItem key={p.name} value={p.name}>
                    {p.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => {
                setDialogJob(null);
                setSelectedProject("");
              }}
            >
              Cancelar
            </Button>
            <Button
              variant={dialogJob?.destructive ? "destructive" : "default"}
              disabled={dispatching}
              onClick={handleDispatch}
            >
              Confirmar disparo
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={mergeDialogOpen}
        onOpenChange={(open) => {
          if (!open) {
            setMergeDialogOpen(false);
            setMergePreview(null);
          }
        }}
      >
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>Projetos duplicados sugeridos</DialogTitle>
          </DialogHeader>
          {mergeLoading && mergePreview === null ? (
            <p className="text-sm text-zinc-400">Analisando com LLM…</p>
          ) : mergePreview && mergePreview.length > 0 ? (
            <div className="max-h-80 space-y-3 overflow-auto">
              {mergePreview.map((group) => (
                <div
                  key={group.canonical}
                  className="rounded-md border border-zinc-800 bg-zinc-900 p-3 text-sm"
                >
                  <p className="font-medium text-zinc-200">
                    Canônico: {group.canonical}
                  </p>
                  <p className="text-zinc-400">
                    Unificar: {group.aliases.join(", ")}
                  </p>
                  <p className="text-zinc-500">
                    Confiança: {(group.confidence * 100).toFixed(0)}% —{" "}
                    {group.reason}
                  </p>
                  <p className="text-xs text-zinc-600">
                    Memórias:{" "}
                    {Object.entries(group.memory_counts)
                      .map(([name, count]) => `${name}: ${count}`)
                      .join(" · ")}
                  </p>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-sm text-zinc-400">
              Nenhum grupo de merge sugerido.
            </p>
          )}
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => {
                setMergeDialogOpen(false);
                setMergePreview(null);
              }}
            >
              Fechar
            </Button>
            <Button
              disabled={mergeLoading || !mergePreview || mergePreview.length === 0}
              onClick={handleMergeProjects}
            >
              Propor unificação
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
