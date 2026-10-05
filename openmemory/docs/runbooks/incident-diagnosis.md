# Runbook — Diagnóstico de incidente

> Prontidão para produção, task_08/09 — ADR-004. Usa alertas Prometheus + traces OTel.

## Fluxo geral

1. **Alerta dispara** (Prometheus/Alertmanager). Identifique qual (abaixo).
2. **Confirme no Grafana** o sintoma (latência, fila, erros).
3. **Abra o trace** da requisição lenta/falha no Tempo, correlacionando pelo
   `trace_id` que aparece nos logs estruturados (campo `trace_id`).
4. **Siga a ação** correspondente.

## Alertas → ação

### `SearchLatencyP99High` (p99 busca > 500ms por 10m)
- Verifique `embed_cache_hit_total` vs `embed_cache_miss_total` (cache frio?).
- No trace, veja qual span domina: `embed`, `qdrant.search` ou rede.
- Ações: aquecer cache, checar saúde do Qdrant/Ollama (`/health`).

### `WriteQueueBacklog` (`write_queue_depth > 100` por 10m)
- Workers de escrita não acompanham. Veja `write_worker_error_total`.
- **ETA aproximado:** `estimated_wait_sec ≈ write_queue_depth × (WRITE_WORKER_EMA_JOB_SEC|45) / (WRITE_WORKER_ETA_CONCURRENCY|1)`.
  Também vem no payload MCP `add_memories` (`queue_depth`, `estimated_wait_sec`).
  Não divida pelo `WRITE_WORKER_MAX_CONCURRENCY` do worker enquanto o LLM for
  serial (llama.cpp `--parallel 1`): isso subestima a fila (~40%+).
- **Drain serial:** se `batch_size < max_concurrency`, o worker só puxava 1 job
  por pass (semaphore inútil). O código agora faz `batch_size = max(batch, concurrency)`.
  Mesmo assim, com LLM `--parallel 1` a extração continua ~serial (~43–50s/job).
- Ações: confirmar concurrency atual e LLM parallel slots; default scale é
  `WRITE_WORKER_MAX_CONCURRENCY=1` alinhado ao LLM. **Não** subir para 2–3+ sem
  (1) aumentar `--parallel` no llama-server e (2) validar isolamento do client
  mem0 sob `add()` concorrente.
- Rollback rápido (só worker — não tocar em `mem0_store` / volumes):

```bash
# Em openmemory/.env ou no shell, forçar concorrência 1:
export WRITE_WORKER_MAX_CONCURRENCY=1
docker compose -f openmemory/docker-compose.scale.yml up -d --no-deps --build openmemory-write-worker
```

- Checar o LLM de extração no trace; `GET /admin/write-queue` e
  `GET /admin/write-queue/worker-status` para backlog / stall.

### `GovernanceJobErrors` / `WriteWorkerErrorRate`
- Inspecione logs filtrando por `job_id`/`request_id`.
- Ações: corrigir causa; jobs têm retry com backoff (`max_attempts`).

### `BackupRPOViolated` (sem backup há > 24h)
- A rotina de backup falhou ou não rodou. Veja `backup_errors_total`.
- Ações: rodar `POST /admin/backup/run`; checar MinIO e credenciais S3; ver
  [runbook de backup](backup-restore.md).

### `ProjectOverSizeThreshold`
- Algum project passou do limite. Veja `GET /admin/projects/sizes`.
- Ações: promover a shard dedicado (`/admin/projects/{name}/promote`) ou definir
  `max_memories`/`enforce` (ver [governança](governance.md)).

## Correlação log ↔ trace

Os logs trazem `request_id`, `job_id` e `trace_id`. Para uma requisição MCP lenta,
copie o `trace_id` do log e abra no Tempo para ver a cadeia
`mcp → embed → qdrant → (escrita) llm`.

## Saúde rápida

```bash
curl localhost:8765/health     # database, qdrant, memory client, fila, rerank status
curl localhost:8765/metrics    # métricas Prometheus (agregadas entre processos; ver prometheus-metrics.md)
curl localhost:8765/admin/deletion-guard
curl localhost:8765/admin/rerank   # configured=false + reason=not_configured é o default
```

### Rerank (opcional — backlog)

Por padrão **não** há modelo de rerank. `search_memory(rerank=true)` responde
`rerank.applied=false` com `reason=not_configured`. Para ligar depois (fora do
caminho crítico):

1. Definir `MEM0_RERANKER_PROVIDER` (`sentence_transformer` ou `cohere`) e
   opcionalmente `MEM0_RERANKER_MODEL` / `MEM0_RERANKER_API_KEY`.
2. Recriar **somente** `openmemory-mcp` (`up -d --no-deps`); não reinicie Qdrant.
3. Confirmar `GET /admin/rerank` e um search com `rerank=true` → `applied=true`.