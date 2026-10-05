// Tipos do espaço compartilhado de specs (task_12) — espelham os schemas
// Pydantic de `openmemory/api/app/routers/specs.py`.

export type SpecWorkspaceStatus =
  | "planejamento"
  | "ativo"
  | "concluido"
  | "arquivado";

export type TaskCardStatus =
  | "tasks"
  | "em_andamento"
  | "revisao_codigo"
  | "fase_teste"
  | "concluido";

export type DocumentType = "prd" | "techspec" | "tasks" | "adrs";

export type CommentTargetType = "workspace" | "document" | "task";

export interface Workspace {
  id: string;
  project_id: string;
  slug: string;
  name: string;
  status: SpecWorkspaceStatus;
  created_by?: string | null;
  created_by_email?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface WorkspaceSummary {
  id: string;
  project_id: string;
  slug: string;
  name: string;
  status: SpecWorkspaceStatus;
  // Contagem de tasks por coluna do quadro (ex.: { tasks: 2, em_andamento: 1 }).
  task_counts: Record<string, number>;
}

export interface SpecDocument {
  id: string;
  workspace_id: string;
  document_type: DocumentType;
  current_version: number;
  current_content?: string | null;
  updated_by?: string | null;
  updated_by_display_name?: string | null;
  updated_by_avatar_url?: string | null;
  updated_at?: string | null;
}

export interface TaskLabelBrief {
  id: string;
  name: string;
  color?: string | null;
}

export interface TaskCard {
  id: string;
  workspace_id: string;
  title: string;
  description?: string | null;
  status: TaskCardStatus;
  is_blocked: boolean;
  block_reason?: string | null;
  assignee?: string | null;
  assignee_display_name?: string | null;
  assignee_avatar_url?: string | null;
  version: number;
  last_activity_at?: string | null;
  branch_ref?: string | null;
  due_at?: string | null;
  position?: number;
  members?: string[];
  label_ids?: string[];
  labels?: TaskLabelBrief[];
  checklist_done?: number;
  checklist_total?: number;
  attachment_count?: number;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface WorkspaceBoard {
  workspace: Workspace;
  documents: SpecDocument[];
  tasks: TaskCard[];
}

export interface DocumentVersion {
  id: string;
  version: number;
  content: string;
  author?: string | null;
  origin: "mcp" | "ui" | "api";
  created_at?: string | null;
}

export interface SpecSearchResult {
  id?: string | null;
  score?: number | null;
  content?: string | null;
  project?: string | null;
  workspace_id?: string | null;
  document_type?: string | null;
  group_id?: string | null;
  owner?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

// --- Requests ---
export interface WorkspaceCreate {
  project_id: string;
  slug: string;
  name: string;
  status?: SpecWorkspaceStatus;
  created_by?: string;
}

export interface DocumentWriteRequest {
  content: string;
  expected_version?: number | null;
  author?: string | null;
}

export interface TaskCreate {
  workspace_id: string;
  title: string;
  description?: string | null;
  branch_ref?: string | null;
}

export interface TaskUpdate {
  expected_version: number;
  title?: string | null;
  description?: string | null;
  branch_ref?: string | null;
  due_at?: string | null;
  clear_due_at?: boolean;
  position?: number | null;
  members?: string[] | null;
}

export interface TaskLabel {
  id: string;
  workspace_id: string;
  name: string;
  color?: string | null;
  created_at?: string | null;
}

export interface ChecklistItem {
  id: string;
  checklist_id: string;
  title: string;
  is_completed: boolean;
  position: number;
  created_at?: string | null;
}

export interface Checklist {
  id: string;
  task_id: string;
  title: string;
  position: number;
  created_at?: string | null;
  items: ChecklistItem[];
}

export interface TaskAttachment {
  id: string;
  task_id: string;
  filename: string;
  content_type?: string | null;
  size_bytes: number;
  uploaded_by?: string | null;
  created_at?: string | null;
}

export interface StatusPatchRequest {
  expected_version: number;
  new_status?: TaskCardStatus;
  actor?: string | null;
  is_blocked?: boolean | null;
  block_reason?: string | null;
}

export interface CommentCreate {
  target_type: CommentTargetType;
  target_id: string;
  body: string;
  author?: string | null;
}

// --- Results (com sinalização de conflito para a UI) ---
export interface DocumentWriteResult {
  document_id?: string;
  version?: number;
  conflict: boolean;
  // Preenchidos quando conflict=true (vêm do detail do HTTP 409).
  current_version?: number;
  current_content?: string | null;
}

export interface ClaimResult {
  claimed: boolean;
  // Presente quando claimed=false (409): responsável atual e versão vigente.
  current_assignee?: string | null;
  version?: number;
  task?: TaskCard;
}

export interface UpdateStatusResult {
  conflict: boolean;
  task?: TaskCard;
  // Preenchidos quando conflict=true (409).
  current_version?: number;
  current_status?: TaskCardStatus;
}
