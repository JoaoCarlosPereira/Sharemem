# AGENTS.md

This file provides context for AI coding assistants (Claude Code, Cursor, GitHub Copilot, Codex, etc.) working with the **ShareMem** repository.

## Project Overview

**ShareMem** (*Shared Memory for AI Engineering Agents*) is a local-first, team-shared memory layer for AI engineering agents. One LAN install gives every agent the same long-lived project memory — without sending content to the cloud.

Runtime surface: **OpenMemory** (FastAPI MCP/API + Next.js UI) on Qdrant + local LLM (Ollama / llama.cpp). The open-source **mem0** SDK packages in this monorepo remain the library foundation under that product surface.

- **Product**: ShareMem — Shared Memory for AI Engineering Agents
- **License**: Apache-2.0
- **Primary docs**: [`README.md`](README.md), [`openmemory/docs/`](openmemory/docs/)

## CRITICAL — Proteção de memórias (OpenMemory / deploy compartilhado)

Este repositório roda **OpenMemory em produção na LAN** (`openmemory/docker-compose.scale.yml`).
As memórias da equipe vivem no **volume Docker `mem0_storage` (Qdrant)** e na fila
durável **`write_queue` (PostgreSQL)**. Perda do volume Qdrant já causou perda de
**1000+ memórias**; a recuperação depende da fila PostgreSQL.

**Qualquer pessoa ou agente de IA que manipule este repositório DEVE seguir estas regras.
Não execute, sugira ou automatize nada abaixo sem pedido explícito do usuário.**

### Infraestrutura Docker — NUNCA apagar volumes por acidente

| Proibido | Use em vez disso |
|----------|------------------|
| `docker compose down -v` | `openmemory/scripts/safe-stack-down.sh` |
| `make down-clean` sem confirmação | `make down` (preserva volumes) |
| `docker volume rm mem0_storage` | — |
| `docker compose up -d` em stack inteira só para rebuild de API | Rebuild **somente** dos serviços alterados: `openmemory-mcp`, `openmemory-write-worker` |

Destruição de volumes **só** com confirmação explícita do usuário:

```bash
CONFIRM_VOLUME_DESTROY=1 make down-clean
# ou
CONFIRM_VOLUME_DESTROY=1 ./openmemory/scripts/safe-stack-down.sh --destroy-volumes
```

**Nunca** reinicie ou recrie o serviço `mem0_store` (Qdrant) sem avisar o usuário e
verificar `points_count` em `http://localhost:6333/collections/openmemory`.

### Aplicação — guarda fail-closed de exclusão

Implementação: `openmemory/api/app/utils/deletion_guard.py`

| Variável | Padrão | Significado |
|----------|--------|-------------|
| `MEM0_ALLOW_MEMORY_DELETE` | `0` | Bloqueia delete por ID (API/MCP) |
| `MEM0_ALLOW_BULK_DELETE` | `0` | Bloqueia `delete_all` e delete em lote |

**Nunca**, sem pedido explícito do usuário:

- Definir `MEM0_ALLOW_MEMORY_DELETE=1` ou `MEM0_ALLOW_BULK_DELETE=1` no `.env`, compose ou config
- Remover, contornar ou enfraquecer `deletion_guard.py` ou suas chamadas em `mcp_server.py` / `routers/memories.py`
- Chamar MCP `delete_all_memories`, `delete_memories` em lote, ou `POST /api/v1/memories/actions/delete`
- Executar `POST /admin/backup/restore` (sobrescreve Qdrant)
- Truncar ou apagar linhas de `write_queue` / `write_audit_logs` no PostgreSQL

Status da guarda: `GET /admin/deletion-guard`

### Recuperação e backups

- Se Qdrant estiver vazio mas `write_queue` tiver jobs `done`, reenfileirar com
  `openmemory/scripts/requeue-done-jobs.py` ou `POST /admin/write-queue/requeue-done`
  (após deploy) — **não** apagar a fila.
- Configure backup periódico: `POST /admin/backup/run` (MinIO bucket `mem0-backups`).

## Repository Structure

This is a **polyglot monorepo** containing Python and TypeScript packages, CLIs, servers, plugins, and documentation.

### Key Directories

| Directory | Description |
|-----------|-------------|
| `mem0/` | Core Python SDK (`mem0ai` on PyPI) — memory, LLMs, embeddings, vector stores, graphs, rerankers |
| `mem0-ts/` | TypeScript SDK (`mem0ai` on npm) — client + OSS memory |
| `cli/python/` | Python CLI (`mem0-cli` on PyPI) — Typer-based, entry point `mem0` |
| `cli/node/` | Node CLI (`@mem0/cli` on npm) — Commander-based, entry point `mem0` |
| `integrations/` | **Agent & editor integrations**, one directory per integration (see "Adding a New Integration") |
| `integrations/mem0-plugin/` | AI editor plugins (Claude Code, Cursor, Codex) — MCP server connection, lifecycle hooks, skills. Contains nested `.opencode-plugin/` (`@mem0/opencode-plugin`) |
| `integrations/openclaw/` | `@mem0/openclaw-mem0` — OpenClaw plugin for Claude Code / AI editors |
| `integrations/pi-agent-plugin/` | `@mem0/pi-agent-plugin` — Pi Agent plugin |
| `integrations/vercel-ai-sdk/` | `@mem0/vercel-ai-provider` — Vercel AI SDK memory provider |
| `server/` | FastAPI REST server for self-hosted Mem0 (Docker: FastAPI + PostgreSQL/pgvector + Neo4j) |
| `openmemory/` | Self-hosted memory platform — `api/` (FastAPI + Alembic + MCP server) and `ui/` (Next.js 15 + React 19) |
| `skills/` | Claude Code skill definitions. Reference skills (SDK knowledge, always-on): `mem0/`, `mem0-cli/`, `mem0-vercel-ai-sdk/`. Pipeline skills (run on demand): `mem0-integrate/`, `mem0-test-integration/`, `mem0-oss-to-platform/` |
| `docs/` | Documentation site (Mintlify) |
| `tests/` | Python SDK tests (pytest) |
| `evaluation/` | Submodule → [`mem0ai/memory-benchmarks`](https://github.com/mem0ai/memory-benchmarks) — benchmarking (LOCOMO, LongMemEval, BEAM) lives in that repo |
| `examples/` | Sample projects & runnable demos — apps, Chrome extension, multi-agent patterns, and Jupyter notebooks (`notebooks/`) |
| `pr-reviews/` | Pull request review materials |
| `scripts/` | Repo-wide utility scripts (e.g., `check-llms-txt-coverage.py` for docs/llms.txt sync) |

### Core Package Dependencies

```
mem0 (Python SDK)          mem0-ts (TypeScript SDK)
├── mem0/memory/           ├── src/client/        (MemoryClient — hosted)
├── mem0/llms/             └── src/oss/           (Memory — self-hosted)
├── mem0/embeddings/           ├── src/llms/
├── mem0/vector_stores/        ├── src/embeddings/
├── mem0/graphs/               ├── src/vector_stores/
└── mem0/reranker/             └── src/graphs/

cli/python/ ──▶ mem0ai (optional, for OSS mode)
cli/node/   ──▶ mem0ai (npm, for API calls)
integrations/vercel-ai-sdk/ ──▶ ai, @ai-sdk/* providers
integrations/openclaw/ ──▶ mem0ai (npm)
```

## Development Setup

### Requirements

- **Python**: 3.9+ (3.10+ for CLI)
- **Node.js**: v18+ (v20 or v22 recommended)
- **pnpm**: v10+ (`npm install -g pnpm@10`) — used for all TypeScript packages
- **Hatch**: Python build/environment tool (`pip install hatch`)
- **Docker**: Required for `server/` and `openmemory/` development

### Initial Setup

```bash
# Python SDK
hatch shell dev_py_3_11           # creates environment with all deps
pre-commit install                # install git hooks

# TypeScript packages
cd mem0-ts && pnpm install        # TS SDK
cd cli/node && pnpm install       # Node CLI
cd integrations/vercel-ai-sdk && pnpm install  # Vercel AI provider
cd integrations/openclaw && pnpm install       # OpenClaw plugin
```

## Build, Lint, and Test Commands

### Python SDK (`mem0/`)

```bash
# Environment setup (uses Hatch)
hatch shell dev_py_3_11           # or dev_py_3_9, dev_py_3_10, dev_py_3_12

# Linting and formatting
make lint                          # ruff check
make format                        # ruff format
make sort                          # isort mem0/

# Tests
make test                          # pytest tests/
make test-py-3.9                   # test specific Python version (3.9–3.12)

# Build and publish
make build                         # hatch build
make publish                       # hatch publish
```

- **Python:** 3.9, 3.10, 3.11, 3.12
- **Linter/formatter:** Ruff (line length **120**)
- **Import sorting:** isort (`profile = "black"`)
- **Test framework:** pytest (with pytest-mock, pytest-asyncio)
- **Pre-commit hooks:** ruff + isort — run `pre-commit install` before committing

### TypeScript SDK (`mem0-ts/`)

```bash
cd mem0-ts
pnpm install
pnpm run build                     # tsup
pnpm run test                      # jest (all tests)
pnpm run test:unit                 # jest --coverage (unit tests only)
pnpm run test:integration          # jest (integration tests, needs MEM0_API_KEY)
pnpm run test:ci                   # jest --coverage --ci (CI mode)
pnpm run test:watch                # jest watch mode
```

- **Node:** 20, 22 (CI-tested)
- **Build:** tsup (CJS + ESM)
- **Test:** jest
- **Formatter:** prettier

### Python CLI (`cli/python/`)

```bash
cd cli/python
pip install -e ".[dev]"            # dev install with ruff + pytest
ruff check .                       # lint
ruff format .                      # format
pytest                             # test
hatch build                        # build
```

- **Python:** 3.10+ (not 3.9)
- **Linter/formatter:** Ruff (line length **100** — different from root SDK)
- **Ruff rules:** E, F, I, W, UP, B, SIM, RUF (ignores E501, B008 for Typer patterns, SIM108)
- **Framework:** Typer + Rich + httpx
- **Entry point:** `mem0 = "mem0_cli.app:main"`
- **Source layout:** `src/mem0_cli/`
- **Optional dependency:** `mem0ai` (for OSS mode, via `[oss]` extra)

### Node CLI (`cli/node/`)

```bash
cd cli/node
pnpm install
pnpm run build                     # tsup
pnpm run lint                      # biome check src/
pnpm run lint:fix                  # biome check --write src/
pnpm run typecheck                 # tsc --noEmit
pnpm run test                      # vitest run
pnpm run test:watch                # vitest (watch mode)
pnpm run dev                       # tsx src/index.ts (development)
```

- **Node:** 18+ required
- **Build:** tsup (ESM)
- **Linter:** Biome (not ESLint, not Ruff)
- **Test:** vitest (not jest)
- **Framework:** Commander + Chalk + ora + cli-table3

### Vercel AI SDK Provider (`integrations/vercel-ai-sdk/`)

```bash
cd integrations/vercel-ai-sdk
pnpm install
pnpm run build                     # tsup
pnpm run lint                      # eslint
pnpm run type-check                # tsc --noEmit
pnpm run prettier-check            # prettier --check
pnpm run test                      # jest
pnpm run test:edge                 # vitest (edge runtime)
pnpm run test:node                 # vitest (node runtime)
```

- **Build:** tsup (CJS + ESM)
- **Lint:** ESLint + Prettier
- **Test:** jest + vitest (edge/node configs)

### OpenClaw Plugin (`integrations/openclaw/`)

```bash
cd integrations/openclaw
pnpm install
pnpm run build                     # tsup
pnpm run test                      # vitest run
```

- **Build:** tsup (ESM)
- **Test:** vitest (with Codecov in CI)
- **Plugin manifest:** `openclaw.plugin.json`

### Server (`server/`)

```bash
# Docker production build
cd server
make build                         # docker build -t mem0-api-server .
make run_local                     # docker run -p 8000:8000 with .env

# Docker Compose development (FastAPI + PostgreSQL/pgvector + Neo4j)
cd server
docker-compose up                  # starts all 3 services
# mem0 API: localhost:8888
# PostgreSQL: localhost:8432
# Neo4j HTTP: localhost:8474, Bolt: localhost:8687
```

- **Framework:** FastAPI with uvicorn (auto-reload in dev)
- **Services:** PostgreSQL with pgvector, Neo4j 5.x with APOC plugin
- **Hot reload:** Dev Dockerfile mounts `server/` and `mem0/` for live changes

### OpenMemory (`openmemory/`)

```bash
# Full stack via Docker Compose
cd openmemory
docker-compose up
# Qdrant: localhost:6333
# API (MCP): localhost:8765
# UI: localhost:3000

# Individual development
cd openmemory/api && uvicorn main:app --reload       # FastAPI backend
cd openmemory/ui && npm run dev                       # Next.js frontend

# Tests
cd openmemory/api && pytest tests/                   # API tests (e.g., test_mcp_server.py)
```

- **API:** FastAPI + Alembic (DB migrations) + MCP server (Model Context Protocol)
- **UI:** Next.js 15, React 19, Radix UI, Redux Toolkit, TailwindCSS, Recharts
- **Vector store:** Qdrant

### Documentation (`docs/`)

```bash
make docs                          # or: cd docs && mintlify dev
```

- **Framework:** Mintlify
- **API spec:** `docs/openapi.json`
- **Structure:** `api-reference/`, `open-source/`, `platform/`, `integrations/`, `cookbooks/`, `core-concepts/`

### Evaluation / Benchmarking

Benchmarking lives in the external [`mem0ai/memory-benchmarks`](https://github.com/mem0ai/memory-benchmarks) repo (LOCOMO + LongMemEval + BEAM). The in-repo `evaluation/` path is a **git submodule** pinned to that repo's `main` — populate it with `git submodule update --init evaluation` (or clone mem0 with `--recurse-submodules`), or clone the benchmarks repo standalone:

```bash
git clone https://github.com/mem0ai/memory-benchmarks.git
cd memory-benchmarks
pip install -r requirements.txt

# Run a benchmark (Mem0 Cloud; use docker compose for OSS)
python -m benchmarks.locomo.run --project-name my-test --backend cloud --mem0-api-key $MEM0_API_KEY
python -m benchmarks.longmemeval.run --project-name my-test --backend cloud --mem0-api-key $MEM0_API_KEY --all-questions
python -m benchmarks.beam.run --project-name my-test --backend cloud --mem0-api-key $MEM0_API_KEY --chat-sizes 100K --conversations 0-9
```

## Core APIs

### Python

| Function / Class | Purpose | Import |
|-----------------|---------|--------|
| `Memory` | Self-hosted memory (sync) | `from mem0 import Memory` |
| `AsyncMemory` | Self-hosted memory (async) | `from mem0 import AsyncMemory` |
| `MemoryClient` | Hosted platform client (sync) | `from mem0 import MemoryClient` |
| `AsyncMemoryClient` | Hosted platform client (async) | `from mem0 import AsyncMemoryClient` |

**Key `Memory` / `MemoryClient` methods:**

| Method | Purpose |
|--------|---------|
| `add(messages, *, user_id, agent_id, run_id, metadata)` | Store a new memory |
| `search(query, *, user_id, agent_id, run_id, limit, filters)` | Search memories |
| `get(memory_id)` | Retrieve a single memory by ID |
| `get_all(*, user_id, agent_id, run_id, limit)` | List all memories |
| `update(memory_id, data)` | Update a memory |
| `delete(memory_id)` | Delete a memory |
| `delete_all(*, user_id, agent_id, run_id)` | Delete all memories |
| `history(memory_id)` | Get change history for a memory |

### TypeScript

| Export | Purpose | Import |
|--------|---------|--------|
| `MemoryClient` | Hosted platform client | `import { MemoryClient } from 'mem0ai'` |
| `Memory` | Self-hosted OSS memory | `import { Memory } from 'mem0ai/oss'` |

## Import Patterns

### Python

| What | Import |
|------|--------|
| Core memory classes | `from mem0 import Memory, AsyncMemory` |
| Platform client | `from mem0 import MemoryClient, AsyncMemoryClient` |
| Configuration | `from mem0.configs.base import MemoryConfig` |
| LLM providers | `from mem0.llms.<provider> import <ProviderLLM>` |
| Embedding providers | `from mem0.embeddings.<provider> import <ProviderEmbedding>` |
| Vector store providers | `from mem0.vector_stores.<provider> import <ProviderVectorStore>` |

### TypeScript

| What | Import |
|------|--------|
| Hosted client | `import { MemoryClient } from 'mem0ai'` |
| OSS memory | `import { Memory } from 'mem0ai/oss'` |
| Specific providers (OSS) | `import { OpenAIEmbedding } from 'mem0ai/oss'` |

## Coding Standards

### File Naming Conventions

- **Python source files:** `snake_case.py` (e.g., `azure_openai.py`, `cohere_reranker.py`)
- **Python test files:** `test_<module>.py` (e.g., `test_memory.py`, `test_main.py`)
- **TypeScript source files:** `snake_case.ts` (e.g., `azure_ai_search.ts`)
- **TypeScript test files:** `<module>.test.ts` (e.g., `memory.test.ts`)
- **Config/manifest files:** `kebab-case` (e.g., `openclaw.plugin.json`, `jest.config.js`)

### Python Conventions

- **Provider pattern:** All providers (LLMs, embeddings, vector stores, graphs, rerankers) inherit from a `base.py` abstract class in their directory. Config classes live in `configs.py`.
- **Pydantic v2** for all data models and configuration.
- **Ruff** is the single linting and formatting tool — no black, no flake8.
  - Root SDK: line length **120**
  - Python CLI: line length **100** with extended rule set (UP, B, SIM, RUF)
- **isort** with `profile = "black"` for import sorting.
- Ruff excludes `openmemory/` from root config.

### TypeScript Conventions

- **Build:** tsup across all packages.
- **Package manager:** pnpm everywhere (no npm, no yarn).
- **TypeScript strict mode** across all packages.
- **Linting varies by package:**

| Package | Linter | Formatter | Test Framework |
|---------|--------|-----------|---------------|
| `mem0-ts/` | — | Prettier | jest |
| `cli/node/` | Biome | Biome | vitest |
| `integrations/vercel-ai-sdk/` | ESLint | Prettier | jest + vitest |
| `integrations/openclaw/` | — | — | vitest |

### Type Checking

Always run type checking after modifying TypeScript code:

```bash
cd <package> && pnpm run typecheck    # or: tsc --noEmit
```

## Architecture

### Provider Pattern

The SDK uses a consistent plugin architecture across 5 categories. Each category has a `base.py` abstract class and concrete provider implementations:

| Category | Count | Examples |
|----------|-------|---------|
| **LLMs** | 24 | OpenAI, Anthropic, AWS Bedrock, Azure OpenAI, Gemini, Groq, Ollama, Together, DeepSeek, vLLM, LiteLLM, LM Studio, xAI |
| **Vector Stores** | 30 | Qdrant, Pinecone, Chroma, Weaviate, Milvus, MongoDB, Redis, Elasticsearch, pgvector, Supabase, Faiss, S3 Vectors |
| **Embeddings** | 15 | OpenAI, Azure OpenAI, Gemini, HuggingFace, FastEmbed, Together, AWS Bedrock, Ollama, Vertex AI |
| **Graph Stores** | 4 | Neo4j, Memgraph, Kuzu, Apache AGE |
| **Rerankers** | 5 | Cohere, HuggingFace, LLM-based, Sentence Transformer, Zero Entropy |

### Two Usage Modes

Self-hosted `Memory` / `AsyncMemory` classes and hosted-platform `MemoryClient` — both in Python and TypeScript.

### Graph Memory

Optional layer on top of vector memory for relationship-aware retrieval. Configured via the `graph` section of `MemoryConfig`.

### MCP Integration

Model Context Protocol support in multiple places:

- **Remote:** MCP server at `mcp.mem0.ai`
- **Local:** MCP server in `openmemory/api/` (FastAPI-based)
- **Plugin:** MCP tools in `integrations/mem0-plugin/` — 9 tools: `add_memory`, `search_memories`, `get_memories`, `get_memory`, `update_memory`, `delete_memory`, `delete_all_memories`, `delete_entities`, `list_entities`

### Plugin & Skills System

- `integrations/mem0-plugin/` provides integrations for Claude Code, Cursor, and Codex via MCP server connections and lifecycle hooks for automatic memory capture.
- `skills/` contains structured skill definitions for AI agents, split into two categories:
  - **Reference skills** (always-on SDK knowledge): `mem0` (Python + TS SDKs, framework integrations), `mem0-cli` (terminal workflows), `mem0-vercel-ai-sdk` (Vercel AI provider).
  - **Pipeline skills** (run on demand): `mem0-integrate` wires Mem0 into an existing repo via a TDD pipeline; `mem0-test-integration` verifies what the integrator produced on the same branch (the two are loosely coupled via `.mem0-integration/` artifacts); `mem0-oss-to-platform` migrates an existing project from Mem0 OSS to the hosted Platform SDK (plan, then execute on approval).

### Adding a New Provider

To add a new LLM, embedding, vector store, or reranker provider:

1. Create `mem0/<category>/<provider_name>.py`
2. Inherit from the abstract base class in `mem0/<category>/base.py`
3. Add configuration to `mem0/<category>/configs.py` (if the category uses one)
4. Register the provider in `mem0/<category>/__init__.py`
5. Add tests in `tests/<category>/<provider_name>/`
6. Add any new dependencies to the appropriate optional group in `pyproject.toml` (never to core `dependencies`)
7. Follow the exact pattern of existing providers in the same category — match method signatures, error handling, and config structure

### Adding a New Integration

Agent/editor integrations live under `integrations/`. Each is a self-contained directory (its own `package.json`/lockfile, build, and tests). To add one:

1. Create `integrations/<name>/` and build the integration there.
2. If it publishes to a registry, set `repository.directory: "integrations/<name>"` in its `package.json` so npm provenance links to the correct subdirectory.
3. Add CI/CD under `.github/workflows/` (`<name>-checks.yml`, `<name>-cd.yml`). Use `integrations/<name>` in `paths:` triggers, `working-directory`, and `cache-dependency-path`. Register the release tag prefix in the `case` block in `release.yml` (keep the bare `v*` arm last). Keep workflow **filenames** stable — npm OIDC trusted publishing is pinned to repo + workflow filename.
4. If it is a Claude Code / editor marketplace plugin, register its path in the five `marketplace.json` files (root + `.claude-plugin/`, `.cursor-plugin/`, `.codex-plugin/`, `.agents/plugins/`).
5. Document it under `docs/integrations/` and add the page to `docs/docs.json` and `docs/llms.txt`.
6. Add rows to the "Key Directories" table and the CI/CD tables in this file.

## CI/CD

### CI Workflows (automated testing)

PR testing is orchestrated by a single entry point: **`ci-gate.yml` (CI Gate)** runs on every PR, detects which packages changed, and invokes only the relevant package workflows below as reusable workflows (`workflow_call`). Its final **`CI Gate`** job aggregates the results (skipped pipelines pass; failed or cancelled ones fail) and is the **only status check that needs to be required** in branch protection. Package workflows keep their own push-to-main and manual triggers; their `pull_request` triggers moved into the gate's path filters.

| Workflow | File | Standalone Triggers | Tests |
|----------|------|---------------------|-------|
| CI Gate | `ci-gate.yml` | All PRs | Routes to and aggregates the workflows below |
| Python SDK | `ci.yml` | Push to main | Ruff lint + pytest on Python 3.10, 3.11, 3.12 |
| TypeScript SDK | `ts-sdk-ci.yml` | Push to main (on `mem0-ts/`) | Prettier + build + jest on Node 20, 22 |
| Python CLI | `cli-python-ci.yml` | Push to main (on `cli/python/`), manual | Ruff lint + pytest + hatch build on Python 3.10, 3.11, 3.12 |
| Node CLI | `cli-node-ci.yml` | Push to main (on `cli/node/`), manual | Biome lint + tsc + vitest + tsup build on Node 20, 22 |
| OpenClaw | `openclaw-checks.yml` | Push to main (on `integrations/openclaw/`), manual | tsc + vitest (with Codecov) + tsup build on Node 20, 22 |
| OpenCode Plugin | `opencode-plugin-checks.yml` | Push to main (on `integrations/mem0-plugin/.opencode-plugin/`), manual | Bun: tsc type-check + build + dist artifact check |
| Pi Agent Plugin | `pi-agent-plugin-checks.yml` | Push to main (on `integrations/pi-agent-plugin/`), manual | tsc + vitest + tsup build (dist artifact check) on Node 20, 22 |
| docs llms.txt | `docs-llms-txt-check.yml` | Manual | `docs/llms.txt` coverage check |

When adding a new package CI workflow: give it `workflow_call` (plus `push`/`workflow_dispatch` as needed, but no `pull_request` trigger), then register it in `ci-gate.yml` — a path filter under the `changes` job, a call job, and an entry in the gate job's `needs` list.

### CD Workflows (automated publishing)

Publishing is routed through a single entry point: **`release.yml` (Release Router)** is the only workflow that listens to `release: published` events. It matches the release tag prefix and dispatches the corresponding package workflow via `workflow_dispatch`, so each release produces exactly one routed run (no skipped runs from the other pipelines).

| Workflow | File | Tag Prefix | Target |
|----------|------|------------|--------|
| Release Router | `release.yml` | (all releases) | dispatches the matching workflow below |
| Python SDK | `cd.yml` | `v*` | PyPI (`mem0ai`) |
| TypeScript SDK | `ts-sdk-cd.yml` | `ts-v*` | npm (`mem0ai`) |
| Python CLI | `cli-python-cd.yml` | `cli-v*` | PyPI (`mem0-cli`) |
| Node CLI | `cli-node-cd.yml` | `cli-node-v*` | npm (`@mem0/cli`) |
| Vercel AI SDK | `vercel-ai-cd.yml` | `vercel-ai-v*` | npm (`@mem0/vercel-ai-provider`) |
| OpenClaw | `openclaw-cd.yml` | `openclaw-v*` | npm (`@mem0/openclaw-mem0`) |
| OpenCode Plugin | `opencode-plugin-cd.yml` | `opencode-v*` | npm (`@mem0/opencode-plugin`) |
| Pi Agent Plugin | `pi-agent-plugin-cd.yml` | `pi-agent-v*` | npm (`@mem0/pi-agent-plugin`) |

- Package CD workflows are `workflow_dispatch`-only (inputs: `tag`, `prerelease`); they check out and build the given tag. Registry trusted-publisher settings stay pinned to each package's own workflow filename.
- All publishing uses **OIDC trusted publishing** — no tokens or secrets required.
- First publish of a new npm package must be done manually; OIDC works for subsequent versions.
- To re-publish a release (e.g. after a registry settings fix), do **not** delete/recreate the GitHub release — manually dispatch the package workflow instead: `gh workflow run <package>-cd.yml --ref refs/tags/<tag> -f tag=<tag>`.
- When adding a new package: add its CD workflow (`workflow_dispatch` with `tag`/`prerelease` inputs), then register its tag prefix in the `case` block in `release.yml`. Keep the bare `v*` arm last.

### Utility Workflows

| Workflow | File | Purpose |
|----------|------|---------|
| Issue Labeler | `issue-labeler.yml` | Automatic issue labeling |
| Stale Bot | `stale.yml` | Marks stale issues and PRs |
| llms.txt Check | `docs-llms-txt-check.yml` | Blocks PRs touching `docs/**/*.mdx` when `docs/llms.txt` is out of sync. Fix locally with `python scripts/check-llms-txt-coverage.py --write`. |

## Task Completion Guidelines

These guidelines outline typical artifacts for different task types. Use judgment to adapt based on scope and context.

### Bug Fixes

1. **Unit tests**: Add tests that would fail without the fix (regression tests)
2. **Implementation**: Fix the bug
3. **Manual verification**: Run the relevant test suite to confirm the fix
4. **Lint**: Run the appropriate linter for the package you modified

### New Features

1. **Implementation**: Build the feature following existing patterns
2. **Unit tests**: Comprehensive test coverage for new functionality
3. **Documentation**: Update relevant docs in `docs/` for public APIs
4. **Examples**: Add usage examples if the feature introduces new user-facing behavior
5. **llms.txt**: Any new `.mdx` page under `docs/` must be linked in `docs/llms.txt` with a scope tag (`[Platform]` / `[OSS]` / `[Both]`) and a `Use when ...` description. The `docs-llms-txt-check.yml` workflow runs on every PR that touches docs and **fails the check** if the index is out of sync. To fix: run `python scripts/check-llms-txt-coverage.py --write` locally to scaffold placeholders under `## Unclassified - needs triage`, then replace the `[TODO: ...]` tags, rewrite descriptions as `Use when ...`, move entries into the right section, and delete the triage heading when empty.

### New Provider (LLM / Embedding / Vector Store / Reranker)

1. **Implementation**: Follow the "Adding a New Provider" steps above
2. **Tests**: Add unit tests matching the pattern of existing providers
3. **Configuration**: Add to the appropriate `configs.py` and `__init__.py`
4. **Dependencies**: Add to the correct optional group in `pyproject.toml`
5. **Documentation**: Add an integration guide in `docs/integrations/`

### Refactoring / Internal Changes

- Unit tests for any changed behavior
- No documentation needed for internal-only changes
- Ensure all existing tests still pass

### When to Deviate

These are guidelines, not rigid rules. Adjust based on:

- **Scope**: Trivial fixes (typos, comments) may not need tests
- **Visibility**: Internal changes may not need documentation
- **Context**: Some changes span multiple categories — use judgment

When uncertain about expected artifacts, ask for clarification.

## Contributing Guidelines

### Workflow

1. Fork and clone the repository.
2. Create a feature branch from `main` (e.g., `feature/my-new-feature`).
3. Make your changes — add tests, docs, and examples as appropriate.
4. Run linting and tests for every package you modified (see commands above).
5. Run `pre-commit install` on first setup — hooks run ruff + isort automatically.
6. Commit with a clear message following [Conventional Commits](https://www.conventionalcommits.org/) (e.g., `feat:`, `fix:`, `docs:`, `refactor:`).
7. Push and open a Pull Request against `main`.

### Pull Request Requirements

Every PR must follow the repo's PR template (`.github/PULL_REQUEST_TEMPLATE.md`):

1. **Linked Issue** — Reference the issue with `Closes #<number>`. If no issue exists, create one first or explain why in the description.
2. **Description** — Explain what the PR does and why it's needed.
3. **Type of Change** — Check the appropriate box:
   - Bug fix / New feature / Breaking change / Refactor / Documentation update
4. **Breaking Changes** — If applicable, describe what breaks and the migration path.
5. **Test Coverage** — Check what applies:
   - Added/updated unit tests
   - Added/updated integration tests
   - Tested manually (describe how)
   - No tests needed (explain why)
6. **Checklist** — All must be checked before merge:
   - [ ] Code follows the project's style guidelines
   - [ ] Self-review performed
   - [ ] Tests added that prove the fix/feature works
   - [ ] New and existing tests pass locally
   - [ ] Documentation updated if needed

### PR Description Template

```markdown
## Linked Issue

Closes #<!-- issue number -->

## Description

<!-- What does this PR do? Why is it needed? -->

## Type of Change

- [ ] Bug fix (non-breaking change that fixes an issue)
- [ ] New feature (non-breaking change that adds functionality)
- [ ] Breaking change (fix or feature that would cause existing functionality to change)
- [ ] Refactor (no functional changes)
- [ ] Documentation update

## Breaking Changes

N/A

## Test Coverage

- [ ] I added/updated unit tests
- [ ] I added/updated integration tests
- [ ] I tested manually (describe below)
- [ ] No tests needed (explain why)

## Checklist

- [ ] My code follows the project's style guidelines
- [ ] I have performed a self-review of my code
- [ ] I have added tests that prove my fix/feature works
- [ ] New and existing tests pass locally
- [ ] I have updated documentation if needed
```

### General Rules

- Follow existing code patterns — don't introduce new frameworks or abstractions without discussion.
- Version bumps go in `pyproject.toml` (Python) or `package.json` (TypeScript).
- For `server/` and `openmemory/` work, use Docker Compose for local development.
- Do NOT use `pip` or `conda` for dependency management — use `hatch` (see `docs/contributing/development.mdx`).

### Contributing Guides

| Task | Guide |
|------|-------|
| Code contributions | `docs/contributing/development.mdx` |
| Documentation contributions | `docs/contributing/documentation.mdx` |
| PR template | `.github/PULL_REQUEST_TEMPLATE.md` |
| Bug reports | `.github/ISSUE_TEMPLATE/bug_report.yml` |
| Feature requests | `.github/ISSUE_TEMPLATE/feature_request.yml` |
| Documentation issues | `.github/ISSUE_TEMPLATE/documentation_issue.yml` |

## Do NOT

- **Apagar ou arriscar perda de memórias OpenMemory** — ver seção **CRITICAL — Proteção de memórias** acima. Em especial: nunca `docker compose down -v`, nunca habilitar `MEM0_ALLOW_*_DELETE` sem pedido explícito, nunca rebuild de stack inteira quando bastam serviços API/worker, nunca recriar `mem0_store` sem aviso.
- Modify CI/CD workflows without explicit approval.
- Add new Python dependencies to the core `dependencies` list in `pyproject.toml` without discussion — use optional dependency groups instead.
- Commit `.env` files, API keys, or credentials.
- Skip pre-commit hooks.
- Use npm or yarn in TypeScript packages — this repo uses pnpm exclusively.
- Use `require()` for imports in TypeScript — use ES module `import` syntax.
- Mix up linter configs: root Python SDK uses line-length 120, Python CLI uses 100, Node CLI uses Biome (not ESLint/Ruff).
- Modify `openmemory/` database migrations without understanding the Alembic migration chain.
- Change public APIs without updating documentation in `docs/`.

## Design Context (from Impeccable)

OpenMemory's visual interface (OpenMemory UI) is treated as a **product register** (an Agent Control Center).
- **Core users:** AI engineering teams and agents operating on a local LAN.
- **Brand Personality:** Modern, clean, and technical (Vercel/Linear feel).
- **Design Principles:** Developer-First Clarity, Local & Fast, Precision over Playfulness.
- **Anti-references:** Avoid bloated enterprise patterns and bubbly colors (or saturated "cream/sand" backgrounds).
See `PRODUCT.md` and `DESIGN.md` in the project root for full strategic and visual token definitions.

## Cursor Cloud specific instructions

These notes cover the non-obvious parts of running/testing this fork in the Cursor Cloud VM. Standard commands live in the sections above; only the caveats are captured here.

### What is present in this checkout

This is a **trimmed fork**. Only `mem0/` (Python SDK), `openmemory/` (api + ui), `integrations/mem0-plugin/`, and `skills/` exist here. `mem0-ts/`, `cli/`, `server/`, `docs/`, and the root `tests/` directory referenced elsewhere in this file are **absent**. The only CI-gated product is `openmemory/api` (see `.github/workflows/openmemory-api-ci.yml`).

### Python environment

- Python deps are installed into a virtualenv at `/workspace/.venv` (created by the startup update script). Run tools via `/workspace/.venv/bin/<tool>` or `source .venv/bin/activate` — `~/.local/bin` is **not** on `PATH`, and there is no Hatch here. `hatch`-based commands in the sections above will not work as-is; use the venv directly.
- **Pin `ruff==0.6.9`** (already pinned in the update script). The repo's ruff configs assume ruff's classic `E`/`F` defaults; newer ruff (0.16+) enables a much broader default rule set and reports 1000+ false failures. `ruff 0.6.9` is what `poetry.lock` and the root Makefile pin.
- System lib `libgeos-dev` is required for the API test dependencies and is baked into the VM snapshot (not in the update script).

### OpenMemory API (`openmemory/api`) — primary product

- Lint: from `openmemory/api`, `ruff check app tests` (uses local `ruff.toml`, must be ruff 0.6.9).
- Tests: run from `openmemory/api` with `PYTHONPATH=/workspace` (repo root) so `import mem0` resolves to the in-repo package, and `OPENAI_API_KEY=test-key` (a placeholder is enough — tests are mocked). Full command: `PYTHONPATH=/workspace OPENAI_API_KEY=test-key /workspace/.venv/bin/pytest tests`. ~946 tests pass with no Docker/network (SQLite + mocks). This suite is the reliable end-to-end signal for API logic.
- Run the server (no Docker): `cd openmemory/api && PYTHONPATH=/workspace OPENAI_API_KEY=test-key /workspace/.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8765`. It uses SQLite `./openmemory.db` (auto-created, gitignored) and creates a default user/app on startup. The memory client is lazy-initialized.
- **Expected offline behavior:** `GET /health` returns `503` because Qdrant (`mem0_store`) is unreachable, and memory add/search need Qdrant + an LLM/embedder. The cloud VM has **no Docker and no external network**, so the full memory pipeline (Qdrant + Ollama/OpenAI) cannot run here. Metadata endpoints backed by SQLite work fully (e.g. `/api/v1/apps/`, `/api/v1/stats/`, `GET`/`PUT /api/v1/config/...`, `/admin/*`).
- **`/api/v1/config/*` requires admin credentials on every method (GET included):** start the server with `ADMIN_TOKEN=<token>` and send `X-Admin-Token: <token>` (or a session JWT; `Bearer local`/team tokens are rejected). Responses always mask secrets (`api_key`, `password`, tokens → `****` + last 4 chars); sending a masked value back in PUT/PATCH keeps the stored secret, and any `****…` value left in the payload after restoration is rejected with `422` (a mask is never persisted).

### OpenMemory UI (`openmemory/ui`)

- Next.js 15 (pnpm). Dev: `pnpm --dir openmemory/ui dev` (or `make ui-dev` from `openmemory/`). Point it at the local API with `NEXT_PUBLIC_API_URL=http://localhost:8765`. Dashboard/Admin/Settings render live data from the API without Docker.

### mem0 SDK (`mem0/`)

- Lint with `/workspace/.venv/bin/ruff check mem0`. Note: `mem0/memory/main.py` and `mem0/utils/logger.py` currently have 4 pre-existing `F`-rule findings; `mem0/` is not CI-gated in this fork.
