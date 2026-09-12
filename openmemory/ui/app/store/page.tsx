"use client";

import type { FormEvent, ReactNode } from "react";
import { useEffect, useMemo, useState } from "react";
import {
  Bot,
  Download,
  FileText,
  PackagePlus,
  Plug,
  Puzzle,
  RefreshCcw,
  Search,
  Send,
  Store,
  TerminalSquare,
  Webhook,
} from "lucide-react";

import { PageHeader } from "@/components/shared/PageHeader";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { useApiSessionReady } from "@/hooks/useApiSessionReady";
import { useRegistryCatalog } from "@/hooks/useRegistryCatalog";
import {
  buildPublishManifest,
  INSTALL_TARGET_LABELS,
  INSTALL_TARGETS,
  REGISTRY_KIND_LABELS,
  REGISTRY_RESOURCE_KINDS,
  registryDependencySummary,
  registryResourceDescription,
  registryResourceNamespace,
  registryResourceSearchText,
  registryResourceTag,
  registryResourceTitle,
  registrySourceSummary,
  downloadSkillPackage,
  deleteSkillPackage,
  downloadHookPackage,
  deleteHookPackage,
  parseHookEvents,
  PACKAGED_KINDS,
  publishHookPackage as publishHookPackageRequest,
  publishSkillPackage as publishSkillPackageRequest,
  validatePublishDraft,
  type InstallRecipe,
  type InstallTarget,
  type PublishDraft,
  type RegistryResource,
  type RegistryResourceKind,
} from "@/lib/registry-client";
import { cn } from "@/lib/utils";

const HIDDEN_ANNOTATION_PREFIXES = [
  "agentregistry.mem0.ai/skill-md",
  "agentregistry.mem0.ai/frontmatter-json",
];

const KIND_ICONS: Record<RegistryResourceKind, typeof Store> = {
  skills: TerminalSquare,
  hooks: Webhook,
  mcpservers: Plug,
  prompts: FileText,
  agents: Bot,
  plugins: Puzzle,
};

/** Starting point for the event map, so the shape is obvious in the form. */
const DEFAULT_HOOK_EVENTS = JSON.stringify(
  {
    PreToolUse: [
      {
        matcher: "Bash",
        hooks: [{ type: "command", command: "python ${HOOK_DIR}/guard.py" }],
      },
    ],
  },
  null,
  2,
);

const DEFAULT_DRAFT: PublishDraft = {
  kind: "skills",
  name: "",
  tag: "latest",
  title: "",
  description: "",
  sourceRepository: "",
  promptContent: "",
  skillContent: "",
  hookContent: "",
  hookEvents: DEFAULT_HOOK_EVENTS,
};

type KindFilter = RegistryResourceKind | "all";

export default function StorePage() {
  const apiSessionReady = useApiSessionReady();
  const {
    resources,
    selectedResource,
    applyResponse,
    installRecipe,
    loading,
    detailLoading,
    publishing,
    installing,
    error,
    publishError,
    installError,
    loadCatalog,
    loadDetail,
    publishManifest,
    requestInstallRecipe,
  } = useRegistryCatalog();

  const [query, setQuery] = useState("");
  const [kindFilter, setKindFilter] = useState<KindFilter>("all");
  const [installTarget, setInstallTarget] = useState<InstallTarget>("cursor");
  const [draft, setDraft] = useState<PublishDraft>(DEFAULT_DRAFT);
  const [manifest, setManifest] = useState(buildPublishManifest(DEFAULT_DRAFT));
  const [formErrors, setFormErrors] = useState<string[]>([]);
  const [skillFiles, setSkillFiles] = useState<File[]>([]);
  const [packageMessage, setPackageMessage] = useState<string | null>(null);

  useEffect(() => {
    if (!apiSessionReady) return;
    void loadCatalog();
  }, [apiSessionReady, loadCatalog]);

  useEffect(() => {
    setManifest(buildPublishManifest(draft));
  }, [draft]);

  const filteredResources = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    return resources.filter((resource) => {
      const matchesKind = kindFilter === "all" || resource.registryKind === kindFilter;
      if (!matchesKind) return false;
      if (!normalizedQuery) return true;
      return registryResourceSearchText(resource).includes(normalizedQuery);
    });
  }, [kindFilter, query, resources]);

  const countsByKind = useMemo(() => {
    return REGISTRY_RESOURCE_KINDS.reduce<Record<RegistryResourceKind, number>>(
      (acc, kind) => {
        acc[kind] = resources.filter((resource) => resource.registryKind === kind).length;
        return acc;
      },
      {
        skills: 0,
        hooks: 0,
        mcpservers: 0,
        prompts: 0,
        agents: 0,
        plugins: 0,
      },
    );
  }, [resources]);

  const handleSelectResource = (resource: RegistryResource) => {
    void loadDetail(
      resource.registryKind,
      resource.metadata.name,
      registryResourceTag(resource),
      resource.metadata.namespace,
    );
  };

  const handlePublish = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const errors = validatePublishDraft(draft, skillFiles.length > 0);
    setFormErrors(errors);
    if (errors.length > 0) return;
    if (draft.kind === "hooks") {
      const parsed = parseHookEvents(draft.hookEvents);
      if ("error" in parsed) {
        setFormErrors([parsed.error]);
        return;
      }
      try {
        const files = skillFiles.length
          ? await Promise.all(skillFiles.map(async (file) => ({
              path: relativeSkillPath(file),
              content: await fileToBase64(file),
              encoding: "base64" as const,
              mode: 0o644,
            })))
          : [{ path: "HOOK.md", content: draft.hookContent, encoding: "utf-8" as const, mode: 0o644 }];
        await publishHookPackageRequest({
          name: draft.name.trim(),
          tag: draft.tag.trim() || "latest",
          title: draft.title.trim(),
          description: draft.description.trim(),
          events: parsed.events,
          files,
        });
        setPackageMessage("Hook publicado com sucesso.");
        setSkillFiles([]);
        await loadCatalog();
      } catch (error) {
        setFormErrors([error instanceof Error ? error.message : "Falha ao publicar o Hook."]);
      }
      return;
    }
    if (draft.kind === "skills" && (skillFiles.length > 0 || draft.skillContent.trim())) {
      try {
        const files = skillFiles.length
          ? await Promise.all(skillFiles.map(async (file) => ({
              path: relativeSkillPath(file),
              content: await fileToBase64(file),
              encoding: "base64" as const,
              mode: 0o644,
            })))
          : [{ path: "SKILL.md", content: draft.skillContent, encoding: "utf-8" as const, mode: 0o644 }];
        await publishSkillPackageRequest({
          name: draft.name.trim(),
          tag: draft.tag.trim() || "latest",
          title: draft.title.trim(),
          description: draft.description.trim(),
          files,
        });
        setPackageMessage("Skill completa publicada com sucesso.");
        setSkillFiles([]);
        await loadCatalog();
      } catch (error) {
        setFormErrors([error instanceof Error ? error.message : "Falha ao publicar a Skill completa."]);
      }
      return;
    }
    await publishManifest(manifest);
  };

  const handleDownload = async () => {
    if (!selectedResource) return;
    const isHook = selectedResource.registryKind === "hooks";
    if (!isHook && selectedResource.registryKind !== "skills") return;
    const download = isHook ? downloadHookPackage : downloadSkillPackage;
    try {
      const blob = await download({
        name: selectedResource.metadata.name,
        tag: registryResourceTag(selectedResource),
        namespace: registryResourceNamespace(selectedResource),
      });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `${selectedResource.metadata.name}-${registryResourceTag(selectedResource)}.zip`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
    } catch (error) {
      setPackageMessage(
        error instanceof Error ? error.message : "Falha ao baixar o pacote completo.",
      );
    }
  };

  const handleEdit = (resource: RegistryResource) => {
    const skillMd = resource.metadata.annotations?.["agentregistry.mem0.ai/skill-md"];
    setDraft({
      kind: resource.registryKind,
      name: resource.metadata.name,
      tag: registryResourceTag(resource),
      title: registryResourceTitle(resource),
      description: registryResourceDescription(resource),
      sourceRepository: "",
      promptContent: "",
      skillContent: typeof skillMd === "string" ? skillMd : "",
      hookContent: "",
      hookEvents: hookEventsDraft(resource),
    });
    setPackageMessage("Modo de edição carregado. Selecione a pasta completa para substituir o pacote.");
  };

  const handleDelete = async (resource: RegistryResource) => {
    if (!window.confirm(`Excluir ${resource.metadata.name}@${registryResourceTag(resource)}?`)) return;
    const isHook = resource.registryKind === "hooks";
    try {
      await (isHook ? deleteHookPackage : deleteSkillPackage)({
        name: resource.metadata.name,
        tag: registryResourceTag(resource),
        namespace: registryResourceNamespace(resource),
      });
      setPackageMessage(isHook ? "Hook excluído com sucesso." : "Skill excluída com sucesso.");
      await loadCatalog();
    } catch (error) {
      setPackageMessage(error instanceof Error ? error.message : "Falha ao excluir o recurso.");
    }
  };

  return (
    <div className="space-y-6 text-slate-200">
      <PageHeader
        icon={Store}
        title="Store"
        description="Catálogo interno de skills, MCPs, prompts, agents e plugins"
      />

      {!apiSessionReady ? (
        <div role="alert" className="rounded-2xl border border-amber-500/30 bg-amber-950/30 p-4 text-sm text-amber-200">
          A Store usa a mesma sessão autenticada do ShareMem. Aguarde a sessão
          ser validada para carregar o catálogo.
        </div>
      ) : null}

      {error ? (
        <div role="alert" className="rounded-2xl border border-red-500/30 bg-red-950/30 p-4 text-sm text-red-200">
          {error}
        </div>
      ) : null}

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_430px]">
        <section className="space-y-4">
          <Card>
            <CardContent className="space-y-4 p-4 md:p-5">
              <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
                <div className="relative min-w-0 flex-1">
                  <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-500" />
                  <Input
                    aria-label="Buscar na Store"
                    value={query}
                    onChange={(event) => setQuery(event.target.value)}
                    placeholder="Buscar por nome, descrição, label ou tipo..."
                    className="border-slate-700 bg-slate-950/80 pl-9 text-slate-100"
                  />
                </div>
                <Button
                  type="button"
                  variant="outline"
                  disabled={!apiSessionReady || loading}
                  onClick={() => void loadCatalog()}
                  className="border-slate-700 bg-slate-900 text-slate-200 hover:bg-slate-800 hover:text-white"
                >
                  <RefreshCcw className={cn("mr-2 h-4 w-4", loading && "animate-spin")} />
                  Atualizar
                </Button>
              </div>

              <div className="flex flex-wrap gap-2" aria-label="Filtros por tipo">
                <KindFilterButton
                  active={kindFilter === "all"}
                  label="Todos"
                  count={resources.length}
                  onClick={() => setKindFilter("all")}
                />
                {REGISTRY_RESOURCE_KINDS.map((kind) => (
                  <KindFilterButton
                    key={kind}
                    active={kindFilter === kind}
                    label={REGISTRY_KIND_LABELS[kind]}
                    count={countsByKind[kind]}
                    onClick={() => setKindFilter(kind)}
                  />
                ))}
              </div>
            </CardContent>
          </Card>

          <div className="grid gap-3 md:grid-cols-2">
            {loading ? (
              <CatalogEmptyState title="Carregando catálogo..." />
            ) : filteredResources.length === 0 ? (
              <CatalogEmptyState title="Nenhum recurso encontrado" />
            ) : (
              filteredResources.map((resource) => (
                <ResourceCard
                  key={`${resource.registryKind}:${registryResourceNamespace(resource)}:${resource.metadata.name}:${registryResourceTag(resource)}`}
                  resource={resource}
                  selected={
                    selectedResource?.registryKind === resource.registryKind &&
                    selectedResource.metadata.name === resource.metadata.name &&
                    registryResourceTag(selectedResource) === registryResourceTag(resource)
                  }
                  onSelect={() => handleSelectResource(resource)}
                />
              ))
            )}
          </div>
        </section>

        <aside className="space-y-4">
          <ResourceDetailCard
            resource={selectedResource}
            loading={detailLoading}
            installTarget={installTarget}
            installing={installing}
            installError={installError}
            installRecipe={installRecipe}
            onInstallTargetChange={setInstallTarget}
            onRequestRecipe={() => {
              if (!selectedResource) return;
              void requestInstallRecipe(
                selectedResource.registryKind,
                selectedResource.metadata.name,
                registryResourceTag(selectedResource),
                installTarget,
              );
            }}
            onDownload={() => void handleDownload()}
            onEdit={() => {
              if (selectedResource) handleEdit(selectedResource);
            }}
            onDelete={() => {
              if (selectedResource) void handleDelete(selectedResource);
            }}
          />

          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="flex items-center gap-2 text-lg text-white">
                <PackagePlus className="h-5 w-5 text-blue-400" />
                Publicar ou atualizar
              </CardTitle>
            </CardHeader>
            <CardContent>
              <form className="space-y-3" onSubmit={handlePublish}>
                <div className="grid grid-cols-2 gap-3">
                  <label className="text-sm font-medium text-slate-300">
                    Tipo
                    <select
                      aria-label="Tipo do recurso"
                      value={draft.kind}
                      onChange={(event) =>
                        setDraft((current) => ({
                          ...current,
                          kind: event.target.value as RegistryResourceKind,
                        }))
                      }
                      className="mt-1 h-10 w-full rounded-md border border-slate-700 bg-slate-950 px-3 text-sm text-slate-100"
                    >
                      {REGISTRY_RESOURCE_KINDS.map((kind) => (
                        <option key={kind} value={kind}>
                          {REGISTRY_KIND_LABELS[kind]}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="text-sm font-medium text-slate-300">
                    Tag
                    <Input
                      aria-label="Tag"
                      value={draft.tag}
                      onChange={(event) =>
                        setDraft((current) => ({ ...current, tag: event.target.value }))
                      }
                      className="mt-1 border-slate-700 bg-slate-950 text-slate-100"
                    />
                  </label>
                </div>

                <label className="block text-sm font-medium text-slate-300">
                  Nome
                  <Input
                    aria-label="Nome do recurso"
                    value={draft.name}
                    onChange={(event) =>
                      setDraft((current) => ({ ...current, name: event.target.value }))
                    }
                    placeholder="minha-skill"
                    className="mt-1 border-slate-700 bg-slate-950 text-slate-100"
                  />
                  <span className="mt-1 block text-xs font-normal text-slate-500">
                    Use letras, números e hífens (ex.: equipe-skill). Evite espaços.
                  </span>
                </label>

                <label className="block text-sm font-medium text-slate-300">
                  Título
                  <Input
                    aria-label="Título"
                    value={draft.title}
                    onChange={(event) =>
                      setDraft((current) => ({ ...current, title: event.target.value }))
                    }
                    className="mt-1 border-slate-700 bg-slate-950 text-slate-100"
                  />
                </label>

                <label className="block text-sm font-medium text-slate-300">
                  Descrição
                  <Textarea
                    aria-label="Descrição"
                    value={draft.description}
                    onChange={(event) =>
                      setDraft((current) => ({
                        ...current,
                        description: event.target.value,
                      }))
                    }
                    className="mt-1 min-h-20 border-slate-700 bg-slate-950 text-slate-100"
                  />
                </label>

                {draft.kind === "prompts" ? (
                  <label className="block text-sm font-medium text-slate-300">
                    Conteúdo do prompt
                    <Textarea
                      aria-label="Conteúdo do prompt"
                      value={draft.promptContent}
                      onChange={(event) =>
                        setDraft((current) => ({
                          ...current,
                          promptContent: event.target.value,
                        }))
                      }
                      className="mt-1 min-h-24 border-slate-700 bg-slate-950 font-mono text-slate-100"
                    />
                  </label>
                ) : draft.kind === "hooks" ? (
                  <>
                    <label className="block text-sm font-medium text-slate-300">
                      Eventos do hook (JSON)
                      <Textarea
                        aria-label="Eventos do hook"
                        value={draft.hookEvents}
                        onChange={(event) =>
                          setDraft((current) => ({
                            ...current,
                            hookEvents: event.target.value,
                          }))
                        }
                        className="mt-1 min-h-40 border-slate-700 bg-slate-950 font-mono text-xs text-slate-100"
                      />
                      <span className="mt-1 block text-xs font-normal text-slate-500">
                        Mapa evento → grupos de matcher, como no settings.json. Use ${"{HOOK_DIR}"} nos
                        comandos para apontar para arquivos do próprio pacote.
                      </span>
                    </label>
                    <label className="block text-sm font-medium text-slate-300">
                      Documentação (HOOK.md)
                      <Textarea
                        aria-label="Conteúdo do hook"
                        value={draft.hookContent}
                        onChange={(event) =>
                          setDraft((current) => ({
                            ...current,
                            hookContent: event.target.value,
                          }))
                        }
                        placeholder={"# Bloqueio de banco externo\n\nO que o hook faz e quando dispara."}
                        className="mt-1 min-h-24 border-slate-700 bg-slate-950 font-mono text-xs text-slate-100"
                      />
                    </label>
                    <label className="block text-sm font-medium text-slate-300">
                      Pasta completa do Hook
                      <Input
                        aria-label="Arquivos completos do Hook"
                        type="file"
                        multiple
                        {...({ webkitdirectory: "", directory: "" } as Record<string, string>)}
                        onChange={(event) => setSkillFiles(Array.from(event.target.files ?? []))}
                        className="mt-1 border-slate-700 bg-slate-950 text-slate-100"
                      />
                      <span className="mt-1 block text-xs font-normal text-slate-500">
                        Selecione os scripts e o HOOK.md. Sem pasta, apenas o HOOK.md acima é publicado.
                      </span>
                    </label>
                  </>
                ) : draft.kind === "skills" ? (
                  <>
                    <label className="block text-sm font-medium text-slate-300">
                      Conteúdo da skill (SKILL.md)
                      <Textarea
                        aria-label="Conteúdo da skill"
                        value={draft.skillContent}
                        onChange={(event) =>
                          setDraft((current) => ({
                            ...current,
                            skillContent: event.target.value,
                          }))
                        }
                        placeholder={"---\nname: minha-skill\ndescription: ...\n---\n\n# Instruções"}
                        className="mt-1 min-h-32 border-slate-700 bg-slate-950 font-mono text-xs text-slate-100"
                      />
                      <span className="mt-1 block text-xs font-normal text-slate-500">
                        Preferido na loja LAN — não exige repositório Git.
                      </span>
                    </label>
                    <label className="block text-sm font-medium text-slate-300">
                      Pasta completa da Skill
                      <Input
                        aria-label="Arquivos completos da Skill"
                        type="file"
                        multiple
                        {...({ webkitdirectory: "", directory: "" } as Record<string, string>)}
                        onChange={(event) => setSkillFiles(Array.from(event.target.files ?? []))}
                        className="mt-1 border-slate-700 bg-slate-950 text-slate-100"
                      />
                      <span className="mt-1 block text-xs font-normal text-slate-500">
                        Selecione todos os arquivos, incluindo SKILL.md. O pacote será armazenado como uma Skill completa.
                      </span>
                    </label>
                  </>
                ) : (
                  <label className="block text-sm font-medium text-slate-300">
                    Repositório de origem
                    <Input
                      aria-label="Repositório de origem"
                      value={draft.sourceRepository}
                      onChange={(event) =>
                        setDraft((current) => ({
                          ...current,
                          sourceRepository: event.target.value,
                        }))
                      }
                      placeholder="https://github.com/org/repo"
                      className="mt-1 border-slate-700 bg-slate-950 text-slate-100"
                    />
                  </label>
                )}

                <label className="block text-sm font-medium text-slate-300">
                  Manifesto gerado
                  <Textarea
                    aria-label="Manifesto YAML"
                    value={manifest}
                    onChange={(event) => setManifest(event.target.value)}
                    className="mt-1 min-h-44 border-slate-700 bg-slate-950 font-mono text-xs text-slate-100"
                  />
                </label>

                {formErrors.length > 0 ? (
                  <div role="alert" className="rounded-xl border border-amber-500/30 bg-amber-950/30 p-3 text-sm text-amber-200">
                    <ul className="list-disc space-y-1 pl-5">
                      {formErrors.map((formError) => (
                        <li key={formError}>{formError}</li>
                      ))}
                    </ul>
                  </div>
                ) : null}

                {publishError ? (
                  <div role="alert" className="rounded-xl border border-red-500/30 bg-red-950/30 p-3 text-sm text-red-200">
                    {publishError}
                  </div>
                ) : null}

                {packageMessage ? (
                  <div role="status" className="rounded-xl border border-emerald-500/30 bg-emerald-950/30 p-3 text-sm text-emerald-200">
                    {packageMessage}
                  </div>
                ) : null}

                {applyResponse?.results?.length ? (
                  <div
                    role="status"
                    className={
                      applyResponse.results.some((result) =>
                        String(result.status).toLowerCase().includes("fail"),
                      )
                        ? "rounded-xl border border-red-500/30 bg-red-950/30 p-3 text-sm text-red-200"
                        : "rounded-xl border border-emerald-500/30 bg-emerald-950/30 p-3 text-sm text-emerald-200"
                    }
                  >
                    {applyResponse.results.map((result) => (
                      <div key={`${result.kind}:${result.name}:${result.tag ?? ""}`}>
                        {result.kind} {result.name}
                        {result.tag ? `@${result.tag}` : ""}: {result.status}
                        {result.error ? ` — ${result.error}` : ""}
                      </div>
                    ))}
                  </div>
                ) : null}

                <Button
                  type="submit"
                  disabled={!apiSessionReady || publishing}
                  className="w-full bg-blue-600 text-white hover:bg-blue-500"
                >
                  <Send className="mr-2 h-4 w-4" />
                  {publishing ? "Publicando..." : "Publicar via /v0/apply"}
                </Button>
              </form>
            </CardContent>
          </Card>
        </aside>
      </div>
    </div>
  );
}

function KindFilterButton({
  active,
  label,
  count,
  onClick,
}: {
  active: boolean;
  label: string;
  count: number;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={cn(
        "rounded-full border px-3 py-1.5 text-sm font-semibold transition-colors",
        active
          ? "border-blue-500/60 bg-blue-500/15 text-blue-200"
          : "border-slate-700 bg-slate-900/70 text-slate-300 hover:border-slate-500 hover:text-white",
      )}
    >
      {label} <span className="text-slate-500">{count}</span>
    </button>
  );
}

function CatalogEmptyState({ title }: { title: string }) {
  return (
    <Card className="md:col-span-2">
      <CardContent className="flex min-h-40 items-center justify-center p-6 text-center text-sm text-slate-500">
        {title}
      </CardContent>
    </Card>
  );
}

function ResourceCard({
  resource,
  selected,
  onSelect,
}: {
  resource: RegistryResource;
  selected: boolean;
  onSelect: () => void;
}) {
  const Icon = KIND_ICONS[resource.registryKind];
  return (
    <button
      type="button"
      onClick={onSelect}
      className={cn(
        "rounded-2xl border bg-slate-950/50 p-4 text-left transition-all hover:border-blue-500/50 hover:bg-slate-900/80",
        selected ? "border-blue-500/60 ring-1 ring-blue-500/30" : "border-slate-800",
      )}
    >
      <div className="flex items-start gap-3">
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border border-blue-500/20 bg-blue-600/10">
          <Icon className="h-5 w-5 text-blue-400" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="mb-2 flex flex-wrap items-center gap-2">
            <Badge className="border-blue-500/30 bg-blue-950/40 text-blue-200">
              {REGISTRY_KIND_LABELS[resource.registryKind]}
            </Badge>
            <Badge variant="outline" className="border-slate-700 text-slate-400">
              {registryResourceTag(resource)}
            </Badge>
          </div>
          <p className="truncate text-base font-bold text-white">
            {registryResourceTitle(resource)}
          </p>
          <p className="mt-1 line-clamp-2 text-sm text-slate-400">
            {registryResourceDescription(resource)}
          </p>
          <p className="mt-3 truncate font-mono text-xs text-slate-500">
            {registryResourceNamespace(resource)}/{resource.metadata.name}
          </p>
        </div>
      </div>
    </button>
  );
}

function ResourceDetailCard({
  resource,
  loading,
  installTarget,
  installing,
  installError,
  installRecipe,
  onInstallTargetChange,
  onRequestRecipe,
  onDownload,
  onEdit,
  onDelete,
}: {
  resource: RegistryResource | null;
  loading: boolean;
  installTarget: InstallTarget;
  installing: boolean;
  installError: string | null;
  installRecipe: InstallRecipe | null;
  onInstallTargetChange: (target: InstallTarget) => void;
  onRequestRecipe: () => void;
  onDownload: () => void;
  onEdit: () => void;
  onDelete: () => void;
}) {
  if (loading) {
    return (
      <Card>
        <CardContent className="p-6 text-sm text-slate-400">
          Carregando detalhe...
        </CardContent>
      </Card>
    );
  }

  if (!resource) {
    return (
      <Card>
        <CardContent className="p-6 text-sm text-slate-400">
          Selecione um recurso do catálogo para ver versão, origem, dependências
          e receita de instalação.
        </CardContent>
      </Card>
    );
  }

  const sourceSummary = registrySourceSummary(resource);
  const dependencySummary = registryDependencySummary(resource);
  // Skills and Hooks ship a package, so they are the kinds with a ZIP to
  // download and a tag to edit or delete from the UI.
  const isPackagedKind = PACKAGED_KINDS.includes(resource.registryKind);
  const hookEvents = hookEventSummary(resource);
  const labels = Object.entries(resource.metadata.labels ?? {});
  const annotations = Object.entries(resource.metadata.annotations ?? {}).filter(
    ([key, value]) =>
      !HIDDEN_ANNOTATION_PREFIXES.some((prefix) => key.startsWith(prefix)) &&
      String(value).length <= 80,
  );
  const Icon = KIND_ICONS[resource.registryKind];
  const steps = installRecipe?.steps ?? [];

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-lg text-white">
          <Icon className="h-5 w-5 text-blue-400" />
          Detalhe
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div>
          <div className="mb-2 flex flex-wrap gap-2">
            <Badge className="border-blue-500/30 bg-blue-950/40 text-blue-200">
              {REGISTRY_KIND_LABELS[resource.registryKind]}
            </Badge>
            <Badge variant="outline" className="border-slate-700 text-slate-400">
              {registryResourceTag(resource)}
            </Badge>
          </div>
          <h2 className="text-xl font-bold text-white">{registryResourceTitle(resource)}</h2>
          <p className="mt-1 text-sm text-slate-400">
            {registryResourceDescription(resource)}
          </p>
          <p className="mt-2 break-all font-mono text-xs text-slate-500">
            {registryResourceNamespace(resource)}/{resource.metadata.name}
          </p>
        </div>

        {hookEvents.length ? (
          <DetailSection title="Eventos">
            {hookEvents.map((summary) => (
              <li key={summary} className="break-all">
                {summary}
              </li>
            ))}
          </DetailSection>
        ) : null}

        <DetailSection title="Origem">
          {sourceSummary.length ? (
            sourceSummary.map((summary) => (
              <li key={summary} className="break-all">
                {summary}
              </li>
            ))
          ) : (
            <li>Nenhuma origem declarada no manifesto.</li>
          )}
        </DetailSection>

        <DetailSection title="Dependências">
          {dependencySummary.length ? (
            dependencySummary.map((summary) => <li key={summary}>{summary}</li>)
          ) : (
            <li>Nenhuma dependência declarada.</li>
          )}
        </DetailSection>

        {labels.length || annotations.length ? (
          <div className="space-y-2">
            <h3 className="text-sm font-semibold uppercase tracking-widest text-slate-500">
              Metadados
            </h3>
            <div className="flex flex-wrap gap-2">
              {[...labels, ...annotations].map(([key, value]) => (
                <Badge
                  key={`${key}:${value}`}
                  variant="outline"
                  className="max-w-full truncate border-slate-700 text-slate-300"
                  title={`${key}=${value}`}
                >
                  {key}={value}
                </Badge>
              ))}
            </div>
          </div>
        ) : null}

        <div className="rounded-2xl border border-slate-800 bg-slate-950/60 p-4">
          <div className="mb-3 flex items-center gap-2 text-sm font-semibold text-slate-200">
            <Download className="h-4 w-4 text-blue-400" />
            Instalação local
          </div>
          <label className="block text-sm text-slate-400">
            Alvo
            <select
              aria-label="Alvo de instalação"
              value={installTarget}
              onChange={(event) =>
                onInstallTargetChange(event.target.value as InstallTarget)
              }
              className="mt-1 h-10 w-full rounded-md border border-slate-700 bg-slate-950 px-3 text-sm text-slate-100"
            >
              {INSTALL_TARGETS.map((target) => (
                <option key={target} value={target}>
                  {INSTALL_TARGET_LABELS[target]}
                </option>
              ))}
            </select>
          </label>
          <Button
            type="button"
            onClick={onRequestRecipe}
            disabled={installing}
            className="mt-3 w-full bg-blue-600 text-white hover:bg-blue-500"
          >
            <Download className="mr-2 h-4 w-4" />
            {installing ? "Gerando receita..." : "Gerar receita de instalação"}
          </Button>
          {isPackagedKind ? (
            <Button
              type="button"
              variant="outline"
              onClick={onDownload}
              className="mt-2 w-full border-slate-700 bg-slate-900 text-slate-200 hover:bg-slate-800 hover:text-white"
            >
              <Download className="mr-2 h-4 w-4" />
              {resource.registryKind === "hooks"
                ? "Baixar Hook completo (.zip)"
                : "Baixar Skill completa (.zip)"}
            </Button>
          ) : null}
          {isPackagedKind ? (
            <div className="mt-2 grid grid-cols-2 gap-2">
              <Button type="button" variant="outline" onClick={onEdit} className="border-slate-700 bg-slate-900 text-slate-200">
                Editar
              </Button>
              <Button type="button" variant="destructive" onClick={onDelete}>
                Excluir
              </Button>
            </div>
          ) : null}
          <p className="mt-2 text-xs text-slate-500">
            Gera os passos para aplicar no host ({INSTALL_TARGET_LABELS[installTarget]}).
            A aplicação automática no disco fica a cargo do agente/usuário.
          </p>
          {installError ? (
            <div
              role="alert"
              className="mt-3 rounded-xl border border-red-500/30 bg-red-950/30 p-3 text-sm text-red-200"
            >
              {installError}
            </div>
          ) : null}
          {steps.length ? (
            <div className="mt-3 space-y-2 rounded-xl border border-emerald-500/30 bg-emerald-950/20 p-3 text-sm text-emerald-100">
              <p className="font-semibold text-emerald-200">
                Receita {installRecipe?.target} — {steps.length} passo(s)
              </p>
              <ol className="list-decimal space-y-1 pl-5 text-slate-200">
                {steps.map((step, index) => (
                  <li key={String(step.id ?? index)} className="break-all">
                    <span className="text-slate-400">{String(step.type ?? "step")}</span>
                    {typeof step.path === "string"
                      ? `: ${step.path}`
                      : typeof step.to === "string"
                        ? `: ${step.to}`
                        : ""}
                  </li>
                ))}
              </ol>
            </div>
          ) : null}
        </div>
      </CardContent>
    </Card>
  );
}

function DetailSection({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <div>
      <h3 className="text-sm font-semibold uppercase tracking-widest text-slate-500">
        {title}
      </h3>
      <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-slate-300">
        {children}
      </ul>
    </div>
  );
}

/** One readable line per event: "PreToolUse · Bash → command: guard.py". */
function hookEventSummary(resource: RegistryResource): string[] {
  if (resource.registryKind !== "hooks") return [];
  const events = (resource.spec as Record<string, unknown> | undefined)?.events;
  if (!events || typeof events !== "object") return [];
  const lines: string[] = [];
  for (const [event, groups] of Object.entries(events as Record<string, unknown>)) {
    if (!Array.isArray(groups)) continue;
    for (const group of groups) {
      const record = group as { matcher?: string; hooks?: Array<Record<string, unknown>> };
      const matcher = record.matcher ? ` · ${record.matcher}` : "";
      for (const entry of record.hooks ?? []) {
        const type = String(entry.type ?? "?");
        const detail =
          entry.command ?? entry.url ?? entry.prompt ?? entry.tool ?? "";
        lines.push(`${event}${matcher} → ${type}${detail ? `: ${detail}` : ""}`);
      }
    }
  }
  return lines;
}

/** Pre-fill the event editor with the hook's published events when editing. */
function hookEventsDraft(resource: RegistryResource): string {
  if (resource.registryKind !== "hooks") return DEFAULT_HOOK_EVENTS;
  const events = (resource.spec as Record<string, unknown> | undefined)?.events;
  if (!events || typeof events !== "object") return DEFAULT_HOOK_EVENTS;
  return JSON.stringify(events, null, 2);
}

function relativeSkillPath(file: File): string {
  const candidate = (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name;
  const parts = candidate.split("/");
  return parts.length > 1 ? parts.slice(1).join("/") : candidate;
}

async function fileToBase64(file: File): Promise<string> {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  const chunkSize = 0x8000;
  for (let index = 0; index < bytes.length; index += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(index, index + chunkSize));
  }
  return btoa(binary);
}
