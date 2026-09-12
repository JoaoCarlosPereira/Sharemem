# ShareMem — Shared Memory for AI Engineering Agents

**ShareMem** is a local-first platform for teams that ship software with AI agents
(Claude Code, Cursor, Codex, and other MCP clients). One LAN install gives the
whole team:

1. **Shared long-term memory** — decisions, conventions, bugs, and learnings
2. **Spec-Driven Development (SDD)** — PRD → TechSpec → Tasks → Kanban, with the
   spec as source of truth over MCP
3. **Internal Skills Store** — discover, publish, and install team skills / MCP
   servers from a private catalog

Nothing leaves your network. LLM and embeddings run on Ollama or llama.cpp.

> Product: **ShareMem** · *Shared Memory for AI Engineering Agents*  
> Runtime: API/MCP + UI · Vectors: Qdrant · Catalog: AgentRegistry · Board: Kanban

## Why ShareMem exists

AI engineering agents are powerful in a single session and weak across a team:
context resets, every machine has its own notes, specs live in chat paste, and
skills don’t circulate. Cloud “memory” products fix sharing by sending proprietary
code and business rules off-network — unacceptable for many orgs.

ShareMem’s objective:

**Give every agent on the LAN the same durable memory, the same Spec-Driven
workflow, and the same internal skill catalog — private, shared by `project`,
and operable at team scale.**

Target: ~200 developers and dozens of MCP agents on self-hosted infra.

## What ShareMem is (three pillars)

### 1. Shared memory

Memories are scoped by **`project`** (not by machine). Any agent on any host
reads and writes the same store. Writes are **async** (durable queue → worker →
LLM extract); reads are **sync and fast** (semantic search + cache). Governance
(TTL, dedup, quarantine, quota, cold tier) keeps quality as volume grows.

| Piece | Role |
|-------|------|
| MCP `add_memories` / `search_memory` / `list_memories` | Agent read/write |
| Fail-closed local LLM (`MEM0_LOCAL_ONLY=1`) | Privacy enforced in code |
| Write queue + write worker | Agents never block on extraction |
| Deletion guard (default off) | Prevent accidental mass delete |

### 2. Spec-Driven Development (SDD)

ShareMem is built for **Spec-Driven Development**: agents don’t invent work from
chat — they create and follow shared specs, mirrored on a team Kanban.

```
Idea → PRD → TechSpec (+ ADRs) → Tasks → claim → code → review → test → done
         │         │                │
         └─────────┴── Spec SoT (MCP/REST) ── mirrored to Kanban UI ─┘
```

| Surface | What it does |
|---------|----------------|
| **SpecWorkspace** | Shared workspace per feature (`project_id` + slug) |
| **Spec documents** | Versioned `prd` / `techspec` / `tasks` / `adrs` (optimistic concurrency) |
| **Task cards** | Kanban pipeline: `tasks` → `em_andamento` → `revisao_codigo` → `fase_teste` → `concluido` (no skipping) |
| **Kanban UI** (`/docs`) | Full-bleed board for humans; agents drive status via MCP |
| **SDD column** | Spec artifacts (PRD, TechSpec, ADRs, task list) stay visible on the board |
| **Skills `cy-*`** | `cy-create-prd`, `cy-create-techspec`, `cy-create-tasks`, `cy-execute-task`, `cy-review-round`, `cy-final-verify`, … |

Iron rule: every meaningful agent action updates the Shared Kanban/workspace in
the **same** turn. Chat and local files are not the source of truth.

### 3. Skills Store (internal catalog)

The **Store** (`/store` in the UI) is a private catalog of skills, MCP servers,
prompts, agents, and plugins — backed by **AgentRegistry** on the LAN.

| Capability | How |
|------------|-----|
| Browse / search | UI Store + MCP `search_catalog` / `get_catalog_resource` |
| Publish skills | MCP publish tools or `integrations/agentregistry/scripts/seed-mem0-skills.py` from `skills/` |
| Install on a host | Install recipes (MCP) so agents apply catalog entries locally |
| Auth | Same ShareMem session as the rest of the UI |

Team-written skills under `skills/` (including the SDD `cy-*` pipeline) are meant
to be published here so every developer and agent can install the same playbooks.

## Also in the product

| Area | Highlights |
|------|------------|
| **Identity** | Google Workspace login on the UI; person / machine / agent layers; agent tokens (`omtk_`); legacy hostname agents still work |
| **Scale** | PostgreSQL + PgBouncer, Redis cache, Traefik, separate write workers, Qdrant partitioning / promote |
| **Governance** | Quarantine, TTL, dedup, semantic consolidate, per-project caps, cold tier |
| **Ops** | CI gate, MinIO/S3 backup+restore, OpenTelemetry, rate limit per `(project, hostname)`, team auth modes |
| **Protection** | Never `docker compose down -v`; deletion fail-closed; see [`AGENTS.md`](AGENTS.md) |

## Design principles

| Principle | What we do | Why |
|-----------|------------|-----|
| **Local-first, fail-closed** | Refuse non-local LLM/embedder; telemetry off | Privacy in code, not convention |
| **Scope by `project`** | Shared memory + specs keyed by project | Same acervo for every host |
| **Spec as SoT** | Agents write specs/tasks via MCP; UI mirrors | Humans and agents see one board |
| **Async writes** | Queue + worker for memory extraction | Agents stay responsive |
| **Catalog on the LAN** | AgentRegistry Store for skills/MCP | Reuse playbooks without the public internet |
| **Operate with confidence** | CI, backup, tracing, rate limits, auth | Production on a trusted LAN |

## Architecture (sketch)

```
Agents (Claude Code, Cursor, Codex, …)
        │  MCP  /mcp/{client_name}/sse/{hostname}   (server name: sharemem)
        ▼
┌──────────────────────────────────────────────┐
│ openmemory-mcp  (ShareMem API/MCP)  :8765    │
│  memory queue · specs · kanban · store proxy │
└───────┬──────────────┬──────────────┬────────┘
        ▼              ▼              ▼
     Qdrant      PostgreSQL      AgentRegistry
   (memories)   (queue, specs,    (skills store)
                 kanban mirror)
        │
   Local LLM (Ollama / llama.cpp)
```

UI (`:3000`): memories, projects, **Kanban** (`/docs`), **Store** (`/store`), admin.
Scale mode adds Traefik, Redis, PgBouncer, write/governance/migration workers.
Details: [`openmemory/docs/self-hosted-scale-architecture.md`](openmemory/docs/self-hosted-scale-architecture.md).

### Critical data protection

Team memories live in Docker volume **`mem0_storage` (Qdrant)** and durable
**`write_queue` (PostgreSQL)**. Losing Qdrant has already cost 1000+ memories;
recovery depends on the Postgres queue. Use `openmemory/scripts/safe-stack-down.sh`
— never `docker compose down -v`.

## Deploy profiles

| Profile | When | Compose / script | DB | Workers |
|---------|------|------------------|----|---------|
| **Local-first** | Dev / small team | `python install.py` | SQLite | Embedded write worker |
| **Scale (Compose)** | LAN, dozens of agents | `openmemory/scripts/bootstrap-scale.sh` → `docker-compose.scale.yml` | PostgreSQL + PgBouncer | API + write-worker |
| **Scale (Swarm)** | Explicit replicas | `docker stack deploy -c docker-stack.yml mem0` | PostgreSQL + PgBouncer | API ×4, write-worker ×8, governance ×1 |

## Quick start (local-first)

**Prerequisites:** Docker + Compose v2, Python 3.8+, local LLM (Ollama and/or llama.cpp).

```bash
python install.py
# optional UI:
python install.py --with-ui
```

```bash
python install.py --ollama-url http://192.168.0.10:11434
python install.py --backend llamacpp --llamacpp-url http://192.168.0.10:8080
python install.py --llm llama3.1:latest --embedder nomic-embed-text --yes
python install.py --data-dir /srv/mem0-data --with-ui
```

Services: `mem0_store` (Qdrant `:6333`), `openmemory-mcp` (`:8765`), optional UI (`:3000`).

> Do **not** use upstream `openmemory/run.sh` (expects `OPENAI_API_KEY`).
> Use `install.py` / `openmemory/install-local-first.sh`.

Guide: [`openmemory/INSTALL-memoria-compartilhada.md`](openmemory/INSTALL-memoria-compartilhada.md).

### Scale + smoke

```bash
cd openmemory
./scripts/bootstrap-scale.sh
docker compose -f docker-compose.scale.yml up -d
./scripts/smoke-memoria-compartilhada.sh
```

## Connect an agent

Point the agent at `/provision` (MCP server key: **`sharemem`**).

**Cursor** (replace `SERVIDOR`):

```
Leia http://SERVIDOR:8765/provision?host=cursor e execute a receita retornada:
escreva o bloco MCP no arquivo indicado, defina as variáveis de ambiente do
campo "env", apresente as 3 opções de modo de memória e grave a escolha em
~/.mem0/settings.json. Confirme cada ação com o usuário antes de executar.
```

Same for `host=claude-code` and `host=codex`.

### MCP tool groups

| Group | Examples | Purpose |
|-------|----------|---------|
| **Memory** | `add_memories`, `search_memory`, `list_memories`, `mark_obsolete` | Shared project memory |
| **SDD / Specs** | `create_spec_workspace`, `write_spec_document`, `read_spec_document`, `search_specs` | PRD / TechSpec / Tasks / ADRs |
| **Kanban tasks** | `create_task`, `claim_task`, `update_task_status`, `list_tasks`, `add_spec_comment` | Pipeline without skipping columns |
| **Store** | `search_catalog`, `get_catalog_resource`, `publish_skill_package`, `publish_hook_package`, `get_install_recipe` | Internal catalog: skills, hooks, MCP servers, prompts, agents, plugins |

`project` is required on memory tools. Spec/Kanban tools use `project_id` + workspace.

### Agent memory modes (`~/.mem0/settings.json`)

| Mode | Behavior |
|------|----------|
| **1. Read + write** | Auto search + capture |
| **2. Read; manual write** | Auto context; write on request (recommended) |
| **3. Manual** | Everything via slash commands and MCP |

## Ops cheat sheet

| Endpoint | Use |
|----------|-----|
| `GET /discovery` | MCP auto-config |
| `GET /provision` | Agent install recipe (`sharemem` MCP block) |
| `GET /health` / `GET /metrics` | Health + Prometheus |
| `POST /admin/backup/{run,restore}` | Qdrant + Postgres → MinIO/S3 |
| `GET/PUT /admin/governance/policies` | Governance policy |
| `POST /admin/migration/*` | Partition migration |
| `GET /admin/deletion-guard` | Deletion guard status |

Essential env: `MEM0_LOCAL_ONLY=1`, `MEM0_TELEMETRY=false`, LLM/embedder URLs,
`DATABASE_URL`, `REDIS_URL` (scale), `AUTH_MODE`, backup S3. See
`openmemory/api/.env.example`.

## Tests

```bash
cd openmemory/api && pytest tests/
pytest tests/memory/test_project_scope.py tests/vector_stores/test_qdrant.py
```

CI: `ci-gate.yml` → `openmemory-api-ci.yml`.

## Docs in this repo

| Path | Content |
|------|---------|
| [`skills/cy-create-prd/references/fluxo-sdd.md`](skills/cy-create-prd/references/fluxo-sdd.md) | SDD flow (PRD → TechSpec → Tasks → execute) |
| [`openmemory/docs/runbooks/`](openmemory/docs/runbooks/) | Backup, auth, governance, incident |
| [`openmemory/INSTALL-memoria-compartilhada.md`](openmemory/INSTALL-memoria-compartilhada.md) | Local-first install |
| [`openmemory/docs/self-hosted-scale-architecture.md`](openmemory/docs/self-hosted-scale-architecture.md) | Scale architecture + status |
| [`apresentacao-openmemory.md`](apresentacao-openmemory.md) | Product walkthrough (PT-BR) |
| [`AGENTS.md`](AGENTS.md) | Contributor / agent guide |

## Technology foundation

ShareMem’s product surface is this repo’s API/MCP, UI, Spec/Kanban, and Store.
Under the hood it still uses open-source **mem0** SDK packages as a library
foundation — extended with `project` scope, durable queues, governance, SDD, and
LAN fail-closed guards.

Upstream mem0 docs (SDK reference only): https://docs.mem0.ai

## Governance and Retention
For details on memory lifecycle, cleanup processes, TTL rules (e.g. 180 days idle limit), and deduplication confidence scores, refer to the policy documentation:
[Governance Policy](openmemory/docs/governance/policy.md).

### Procedimento de Revisão (Review Procedure)
- Para alterar as políticas (como TTL ou limites de deduplicação), modifique o arquivo `openmemory/docs/governance/policy.md` e a variável `DEFAULT_POLICY` em `openmemory/api/app/utils/governance_policy.py`.
- O código da aplicação de governança baseia-se num sistema *fail-closed* contra exclusões, alertando agressivamente se as variáveis de ambiente de deleção forem ligadas em produção.
- Use a API `/admin/governance/policy` para alterar sob-demanda em tempo de execução para projetos específicos.

## License

Apache 2.0 — see [LICENSE](LICENSE).
