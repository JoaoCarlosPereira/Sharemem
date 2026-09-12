import axios from "axios";

import { apiClient } from "@/lib/api-client";

export const REGISTRY_RESOURCE_KINDS = [
  "skills",
  "hooks",
  "mcpservers",
  "prompts",
  "agents",
  "plugins",
] as const;

export type RegistryResourceKind = (typeof REGISTRY_RESOURCE_KINDS)[number];

export const REGISTRY_KIND_LABELS: Record<RegistryResourceKind, string> = {
  skills: "Skills",
  hooks: "Hooks",
  mcpservers: "MCP servers",
  prompts: "Prompts",
  agents: "Agents",
  plugins: "Plugins",
};

export const REGISTRY_KIND_API_KIND: Record<RegistryResourceKind, string> = {
  skills: "Skill",
  hooks: "Hook",
  mcpservers: "MCPServer",
  prompts: "Prompt",
  agents: "Agent",
  plugins: "Plugin",
};

export interface RegistryResource {
  apiVersion?: string;
  kind?: string;
  registryKind: RegistryResourceKind;
  metadata: {
    namespace?: string;
    name: string;
    tag?: string;
    labels?: Record<string, string>;
    annotations?: Record<string, string>;
    createdAt?: string;
    updatedAt?: string;
  };
  spec?: Record<string, unknown>;
  status?: unknown;
}

export interface RegistryListResponse {
  items?: RegistryResource[];
  nextCursor?: string;
}

export interface RegistryApplyResult {
  apiVersion?: string;
  kind?: string;
  namespace?: string;
  name: string;
  tag?: string;
  status: string;
  error?: string;
}

export interface RegistryApplyResponse {
  results?: RegistryApplyResult[];
}

/** One matcher group of a hook event, as published to the catalog. */
export interface HookMatcherGroup {
  matcher?: string;
  hooks: Array<Record<string, unknown>>;
}

/** Kinds whose publication is a complete directory plus metadata. */
export const PACKAGED_KINDS: RegistryResourceKind[] = ["skills", "hooks"];

/** The root file each packaged kind requires. */
export const PACKAGE_ROOT_FILE: Partial<Record<RegistryResourceKind, string>> = {
  skills: "SKILL.md",
  hooks: "HOOK.md",
};

export interface PublishDraft {
  kind: RegistryResourceKind;
  name: string;
  tag: string;
  title: string;
  description: string;
  sourceRepository: string;
  promptContent: string;
  /** Inline SKILL.md body (mem0 catalog) — preferred over git for LAN store. */
  skillContent: string;
  /** Inline HOOK.md body, the Hook counterpart of skillContent. */
  hookContent: string;
  /** Event map as JSON text, edited as-is and parsed on submit. */
  hookEvents: string;
}

export function buildRegistryResourcePath(
  kind: RegistryResourceKind,
  name?: string,
  tag?: string,
): string {
  const segments = ["/registry-api/v0", kind];
  if (name) segments.push(encodeURIComponent(name));
  if (tag) segments.push(encodeURIComponent(tag));
  return segments.join("/");
}

export async function listRegistryResources(
  kind: RegistryResourceKind,
): Promise<RegistryResource[]> {
  const response = await apiClient.get<RegistryListResponse>(
    buildRegistryResourcePath(kind),
    {
      params: {
        namespace: "all",
        // Do NOT pass latestOnly: the registry treats it as tag=latest literally,
        // which hides catalog entries published as 1.0.0 / semver (mem0 skills).
        limit: 100,
      },
    },
  );
  return dedupeLatestResources(
    normalizeRegistryItems(kind, response.data?.items ?? []),
  );
}

/** Keep one row per (namespace, name) — prefer most recently updated tag. */
export function dedupeLatestResources(
  items: RegistryResource[],
): RegistryResource[] {
  const best = new Map<string, RegistryResource>();
  for (const item of items) {
    const key = `${registryResourceNamespace(item)}:${item.metadata.name}`;
    const prev = best.get(key);
    if (!prev) {
      best.set(key, item);
      continue;
    }
    const prevTs =
      Date.parse(prev.metadata.updatedAt || prev.metadata.createdAt || "") || 0;
    const nextTs =
      Date.parse(item.metadata.updatedAt || item.metadata.createdAt || "") || 0;
    if (nextTs >= prevTs) {
      best.set(key, item);
    }
  }
  return Array.from(best.values()).sort((a, b) =>
    registryResourceTitle(a).localeCompare(registryResourceTitle(b), "pt"),
  );
}

export async function listAllRegistryResources(): Promise<RegistryResource[]> {
  const grouped = await Promise.all(
    REGISTRY_RESOURCE_KINDS.map((kind) => listRegistryResources(kind)),
  );
  return grouped.flat();
}

export async function getRegistryResource(
  kind: RegistryResourceKind,
  name: string,
  tag?: string,
  namespace?: string,
): Promise<RegistryResource> {
  const response = await apiClient.get<RegistryResource>(
    buildRegistryResourcePath(kind, name, tag),
    namespace ? { params: { namespace } } : undefined,
  );
  return normalizeRegistryItem(kind, response.data);
}

export async function publishRegistryManifest(
  manifest: string,
): Promise<RegistryApplyResponse> {
  const response = await apiClient.post<RegistryApplyResponse>(
    "/registry-api/v0/apply",
    manifest,
    {
      headers: {
        "content-type": "application/yaml",
      },
    },
  );
  return response.data;
}

export const INSTALL_TARGETS = ["cursor", "claude", "codex"] as const;
export type InstallTarget = (typeof INSTALL_TARGETS)[number];

export const INSTALL_TARGET_LABELS: Record<InstallTarget, string> = {
  cursor: "Cursor",
  claude: "Claude Code",
  codex: "Codex",
};

/** mem0 LAN catalog embeds the full SKILL.md in this annotation. */
export const SKILL_MD_ANNOTATION = "agentregistry.mem0.ai/skill-md";

export interface InstallRecipe {
  version: string;
  resource_kind: string;
  name: string;
  tag: string;
  target: InstallTarget | string;
  user_id?: string;
  steps?: Array<Record<string, unknown>>;
  rollback?: Array<Record<string, unknown>>;
  source?: Record<string, unknown>;
  resource?: RegistryResource;
}

const REGISTRY_KIND_TO_RECIPE_KIND: Record<RegistryResourceKind, string> = {
  skills: "skill",
  hooks: "hook",
  mcpservers: "mcpserver",
  prompts: "prompt",
  agents: "agent",
  plugins: "plugin",
};

export async function fetchInstallRecipe(input: {
  kind: RegistryResourceKind;
  name: string;
  tag: string;
  target: InstallTarget;
}): Promise<InstallRecipe> {
  const response = await apiClient.post<InstallRecipe>(
    "/api-proxy/api/v1/store/install-recipes",
    {
      kind: REGISTRY_KIND_TO_RECIPE_KIND[input.kind],
      name: input.name,
      tag: input.tag,
      target: input.target,
    },
  );
  return response.data;
}

export async function downloadSkillPackage(input: {
  name: string;
  tag: string;
  namespace?: string;
}): Promise<Blob> {
  const response = await apiClient.get<Blob>(
    `/registry-api/v0/skills/${encodeURIComponent(input.name)}/${encodeURIComponent(input.tag)}/download`,
    {
      params: { namespace: input.namespace || "default" },
      responseType: "blob",
    },
  );
  return response.data;
}

export async function deleteSkillPackage(input: {
  name: string;
  tag: string;
  namespace?: string;
}): Promise<void> {
  await apiClient.delete(
    `/registry-api/v0/skills/${encodeURIComponent(input.name)}/${encodeURIComponent(input.tag)}`,
    { params: { namespace: input.namespace || "default" } },
  );
}

export async function publishSkillPackage(input: {
  name: string;
  tag: string;
  title?: string;
  description: string;
  language?: "pt-BR";
  files: Array<{
    path: string;
    content: string;
    encoding: "utf-8" | "base64";
    mode?: number;
  }>;
}): Promise<Record<string, unknown>> {
  const response = await apiClient.put<Record<string, unknown>>(
    `/api-proxy/api/v1/store/skills/${encodeURIComponent(input.name)}/${encodeURIComponent(input.tag)}`,
    { ...input, language: input.language || "pt-BR" },
  );
  return response.data;
}

export async function downloadHookPackage(input: {
  name: string;
  tag: string;
  namespace?: string;
}): Promise<Blob> {
  const response = await apiClient.get<Blob>(
    `/registry-api/v0/hooks/${encodeURIComponent(input.name)}/${encodeURIComponent(input.tag)}/download`,
    {
      params: { namespace: input.namespace || "default" },
      responseType: "blob",
    },
  );
  return response.data;
}

export async function deleteHookPackage(input: {
  name: string;
  tag: string;
  namespace?: string;
}): Promise<void> {
  await apiClient.delete(
    `/registry-api/v0/hooks/${encodeURIComponent(input.name)}/${encodeURIComponent(input.tag)}`,
    { params: { namespace: input.namespace || "default" } },
  );
}

export async function publishHookPackage(input: {
  name: string;
  tag: string;
  title?: string;
  description: string;
  language?: "pt-BR";
  events: Record<string, HookMatcherGroup[]>;
  files: Array<{
    path: string;
    content: string;
    encoding: "utf-8" | "base64";
    mode?: number;
  }>;
}): Promise<Record<string, unknown>> {
  const response = await apiClient.put<Record<string, unknown>>(
    `/api-proxy/api/v1/store/hooks/${encodeURIComponent(input.name)}/${encodeURIComponent(input.tag)}`,
    { ...input, language: input.language || "pt-BR" },
  );
  return response.data;
}

/** Parse the draft's event map, returning either the value or a message. */
export function parseHookEvents(
  text: string,
): { events: Record<string, HookMatcherGroup[]> } | { error: string } {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return { error: "O mapa de eventos precisa ser um JSON válido." };
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    return { error: "O mapa de eventos precisa ser um objeto evento → grupos." };
  }
  const events = parsed as Record<string, unknown>;
  if (Object.keys(events).length === 0) {
    return { error: "Informe ao menos um evento." };
  }
  for (const [event, groups] of Object.entries(events)) {
    if (!Array.isArray(groups) || groups.length === 0) {
      return { error: `O evento ${event} precisa de ao menos um grupo de matcher.` };
    }
    for (const group of groups) {
      const hooks = (group as HookMatcherGroup)?.hooks;
      if (!Array.isArray(hooks) || hooks.length === 0) {
        return { error: `Um grupo do evento ${event} está sem entradas em "hooks".` };
      }
    }
  }
  return { events: events as Record<string, HookMatcherGroup[]> };
}

export function normalizeRegistryItems(
  kind: RegistryResourceKind,
  items: RegistryResource[],
): RegistryResource[] {
  return items.map((item) => normalizeRegistryItem(kind, item));
}

export function normalizeRegistryItem(
  kind: RegistryResourceKind,
  item: RegistryResource,
): RegistryResource {
  return {
    ...item,
    registryKind: item.registryKind ?? kind,
    metadata: {
      ...item.metadata,
      name: item.metadata?.name ?? "",
    },
    spec: item.spec ?? {},
  };
}

export function registryResourceTitle(resource: RegistryResource): string {
  const specTitle = resource.spec?.title;
  return typeof specTitle === "string" && specTitle.trim()
    ? specTitle
    : resource.metadata.name;
}

export function registryResourceDescription(resource: RegistryResource): string {
  const description = resource.spec?.description;
  return typeof description === "string" && description.trim()
    ? description
    : "Sem descrição publicada.";
}

export function registryResourceTag(resource: RegistryResource): string {
  return resource.metadata.tag || "latest";
}

export function registryResourceNamespace(resource: RegistryResource): string {
  return resource.metadata.namespace || "default";
}

export function registryResourceSearchText(resource: RegistryResource): string {
  const labels = resource.metadata.labels
    ? Object.entries(resource.metadata.labels)
        .map(([key, value]) => `${key}=${value}`)
        .join(" ")
    : "";
  return [
    REGISTRY_KIND_LABELS[resource.registryKind],
    resource.kind,
    resource.metadata.name,
    registryResourceTitle(resource),
    registryResourceDescription(resource),
    labels,
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
}

export function registrySourceSummary(resource: RegistryResource): string[] {
  const spec = resource.spec ?? {};
  const source = asRecord(spec.source);
  const remote = asRecord(spec.remote);
  const status = asRecord(resource.status);
  const resolvedSource = asRecord(status?.resolvedSource);
  const artifact = asRecord(resolvedSource?.artifact);
  const summaries: string[] = [];

  const annotations = resource.metadata.annotations ?? {};
  const skillMd = annotations[SKILL_MD_ANNOTATION];
  if (typeof skillMd === "string" && skillMd.trim()) {
    summaries.push("Conteúdo embutido (SKILL.md)");
  }
  if (typeof artifact?.digest === "string" && artifact.digest.trim()) {
    const size = typeof artifact.size === "number" ? `, ${artifact.size} bytes` : "";
    summaries.push(`Pacote completo AgentRegistry (SHA-256 ${artifact.digest}${size})`);
  }

  const repository = asRecord(source?.repository);
  const pluginGitRepository = asRecord(asRecord(asRecord(source?.git)?.repository));
  const packageSource = asRecord(source?.package);
  const packageOrigin = asRecord(packageSource?.origin);
  const agentImage = spec.source ? asRecord(spec.source)?.image : undefined;
  const pluginOci = asRecord(source?.oci);

  appendRepositorySummary(summaries, repository);
  appendRepositorySummary(summaries, pluginGitRepository);

  if (typeof agentImage === "string" && agentImage.trim()) {
    summaries.push(`Imagem: ${agentImage}`);
  }
  if (typeof packageOrigin?.identifier === "string" && packageOrigin.identifier.trim()) {
    summaries.push(`Pacote ${packageOrigin.type ?? ""}: ${packageOrigin.identifier}`);
  }
  if (typeof remote?.url === "string" && remote.url.trim()) {
    summaries.push(`Remoto ${remote.type ?? ""}: ${remote.url}`);
  }
  if (typeof pluginOci?.reference === "string" && pluginOci.reference.trim()) {
    summaries.push(`OCI: ${pluginOci.reference}`);
  }

  return summaries;
}

export function registryDependencySummary(resource: RegistryResource): string[] {
  const spec = resource.spec ?? {};
  const summaries: string[] = [];

  appendRefs(summaries, "Skill", spec.skills);
  appendRefs(summaries, "MCP", spec.mcpServers);
  appendRefs(summaries, "Plugin", spec.plugins);

  const instructions = asRecord(spec.instructions);
  if (instructions?.name) {
    summaries.push(`Instruções: ${String(instructions.name)}`);
  }

  return summaries;
}

export function validatePublishDraft(draft: PublishDraft, hasCompleteFiles = false): string[] {
  const errors: string[] = [];
  if (!REGISTRY_RESOURCE_KINDS.includes(draft.kind)) {
    errors.push("Escolha um tipo de recurso válido.");
  }
  if (!draft.name.trim()) {
    errors.push("Informe o nome do recurso.");
  }
  if (!draft.title.trim()) {
    errors.push("Informe um título curto.");
  }
  if (draft.kind === "prompts") {
    if (!draft.promptContent.trim()) {
      errors.push("Informe o conteúdo do prompt.");
    }
  } else if (draft.kind === "skills") {
    if (!hasCompleteFiles && !draft.skillContent.trim()) {
      errors.push("Informe o conteúdo completo da Skill, incluindo SKILL.md.");
    }
  } else if (draft.kind === "hooks") {
    if (!hasCompleteFiles && !draft.hookContent.trim()) {
      errors.push("Informe o conteúdo completo do Hook, incluindo HOOK.md.");
    }
    const parsed = parseHookEvents(draft.hookEvents);
    if ("error" in parsed) {
      errors.push(parsed.error);
    }
  } else if (!draft.sourceRepository.trim()) {
    errors.push("Informe a URL do repositório de origem.");
  }
  return errors;
}

export function buildPublishManifest(draft: PublishDraft): string {
  const tag = draft.tag.trim() || "latest";
  const apiKind = REGISTRY_KIND_API_KIND[draft.kind];
  const title = yamlScalar(draft.title.trim());
  const description = yamlScalar(draft.description.trim());
  const name = yamlScalar(draft.name.trim());
  const tagValue = yamlScalar(tag);
  const repositoryUrl = yamlScalar(draft.sourceRepository.trim());
  const promptContent = yamlBlock(draft.promptContent.trim());
  const skillContent = yamlBlock(draft.skillContent.trim());

  const lines = [
    "apiVersion: ar.dev/v1alpha1",
    `kind: ${apiKind}`,
    "metadata:",
    `  name: ${name}`,
    `  tag: ${tagValue}`,
  ];

  if (draft.kind === "skills" && draft.skillContent.trim()) {
    lines.push(
      "  annotations:",
      `    ${SKILL_MD_ANNOTATION}: |`,
      skillContent,
    );
  }

  lines.push("spec:", `  title: ${title}`);

  if (draft.description.trim()) {
    lines.push(`  description: ${description}`);
  }

  if (draft.kind === "prompts") {
    lines.push("  content: |", promptContent);
    return lines.join("\n") + "\n";
  }

  if (draft.kind === "skills") {
    // Skills are self-contained packages; Git is deliberately not part of the
    // publication contract.
    return lines.join("\n") + "\n";
  }

  if (draft.kind === "plugins") {
    lines.push(
      "  source:",
      "    type: git",
      "    git:",
      "      repository:",
      `        url: ${repositoryUrl}`,
    );
    return lines.join("\n") + "\n";
  }

  lines.push("  source:", "    repository:", `      url: ${repositoryUrl}`);
  return lines.join("\n") + "\n";
}

export function registryErrorMessage(error: unknown, fallback: string): string {
  if (axios.isAxiosError(error)) {
    const detail = error.response?.data?.detail;
    if (typeof detail === "string" && detail.trim()) return detail;
    if (error.response?.status === 401) {
      return "Sessão inválida ou ausente para acessar a Store.";
    }
    if (error.response?.status === 403) {
      return "Seu usuário não tem permissão para esta ação da Store.";
    }
    if (error.message) return error.message;
  }
  if (error instanceof Error && error.message) return error.message;
  return fallback;
}

function appendRepositorySummary(
  summaries: string[],
  repository: Record<string, unknown> | undefined,
) {
  if (!repository || typeof repository.url !== "string" || !repository.url.trim()) {
    return;
  }
  const suffix = [repository.branch, repository.commit, repository.subfolder]
    .filter((value): value is string => typeof value === "string" && !!value.trim())
    .join(" · ");
  summaries.push(`Repositório: ${repository.url}${suffix ? ` (${suffix})` : ""}`);
}

function appendRefs(summaries: string[], label: string, refs: unknown) {
  if (!Array.isArray(refs)) return;
  for (const ref of refs) {
    const record = asRecord(ref);
    const name = record?.name;
    if (typeof name === "string" && name.trim()) {
      const tag = typeof record?.tag === "string" && record.tag.trim() ? `@${record.tag}` : "";
      summaries.push(`${label}: ${name}${tag}`);
    }
  }
}

function asRecord(value: unknown): Record<string, unknown> | undefined {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  return undefined;
}

function yamlScalar(value: string): string {
  return JSON.stringify(value);
}

function yamlBlock(value: string): string {
  const content = value || " ";
  return content
    .split("\n")
    .map((line) => `    ${line}`)
    .join("\n");
}
