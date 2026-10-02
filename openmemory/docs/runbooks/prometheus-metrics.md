# Runbook — métricas Prometheus (`/metrics`) em modo multiprocesso

> Card 0d30e385. Código: `api/app/utils/prometheus_multiproc.py`,
> `api/app/routers/ops_metrics.py`, `api/app/utils/metrics.py`,
> `api/docker-entrypoint.sh`.

## Problema

Por padrão o `prometheus_client` guarda os valores em memória, **por processo**.
Com mais de um processo Python atendendo a API (ex.: `uvicorn --workers N`), o
`/metrics` mostra só o processo que respondeu ao scrape — numa medição real
`mcp_search_latency_seconds` veio zerada. Hoje a API roda `--workers 1`
(sessão SSE de `/mcp/messages` é por processo), então funcionava "por acaso".

## Como funciona

| `PROMETHEUS_MULTIPROC_DIR` | Comportamento de `/metrics` |
|----------------------------|-----------------------------|
| ausente (default fora do compose) | idêntico ao histórico: `generate_latest()` do registry padrão do processo, incluindo `process_*`/`python_*` |
| definido | cada processo grava `<tipo>_<pid>.db` no diretório; `/metrics` monta um `CollectorRegistry` novo com `MultiProcessCollector` e **agrega** todos os processos |

A variável precisa existir **antes** de o processo Python iniciar (o backend é
escolhido no import do `prometheus_client`); não adianta defini-la em runtime.

### Gauges — `multiprocess_mode` por métrica

Counters e Histograms são somados automaticamente. Gauges usam modo explícito
(o default `all` criaria uma série por `pid` e quebraria alertas como
`write_queue_depth > 100`):

| Gauge | Modo | Por quê |
|-------|------|---------|
| `write_queue_depth`, `governance_job_queue_depth` | `livemostrecent` | snapshot de estado atual publicado por loop vivo; processo morto sai do agregado |
| `project_memory_count`, `project_size_over_threshold`, `governance_quota_over_limit_projects`, `governance_revert_rate`, `retrieval_duplicate_in_topk_ratio`, `retrieval_quality_index`, `backup_duration_seconds` | `mostrecent` | snapshot calculado sob demanda; vale o último `set` de qualquer processo |
| `backup_last_success_timestamp` | `max` | timestamp monotônico |
| `governance_quarantined_current` | `sum` | alterado por `inc()` (`mostrecent` não aceita `inc`) |

Sem a variável os modos são ignorados. Ao criar um Gauge novo, **declare**
`multiprocess_mode` (há teste que falha caso contrário).

### Ciclo de vida dos arquivos

- **Start do container:** `docker-entrypoint.sh` (ENTRYPOINT da imagem) apaga
  `*.db` de primeiro nível do diretório antes de qualquer processo Python
  (alembic, uvicorn, workers). Recusa `PROMETHEUS_MULTIPROC_DIR=/`.
- **Saída de processo:** `mark_process_dead` via `atexit` (registrado no import
  de `app.utils.metrics`) e no shutdown do FastAPI. Remove só os Gauges `live*`
  daquele PID; counters/histogramas permanecem para não "voltarem no tempo".
- **SIGKILL/OOM:** nenhum gancho roda; arquivos de Gauges `live*` do PID morto
  ficam até o próximo restart do container (que limpa o diretório).

## Deploy (`docker-compose.scale.yml`)

- Somente `openmemory-mcp` define `PROMETHEUS_MULTIPROC_DIR=/tmp/prometheus-multiproc`
  com `tmpfs` próprio (64 MB). Não é volume nomeado, não persiste entre
  recriações, não é compartilhado com outros serviços.
- **Escopo (decisão):** os workers (`openmemory-write-worker`, governança,
  backup, migração) **não** definem a variável. Eles não expõem `/metrics` e
  rodam em outros containers — tmpfs não é compartilhável entre containers, e um
  volume compartilhado arriscaria um serviço limpar os arquivos do outro no
  start. Logo, métricas observadas *apenas* nesses workers
  (`write_worker_success_total`, `write_worker_error_total`, `write_queue_depth`
  publicada pelo worker standalone, `governance_*`, `backup_*` do backup-worker,
  `migration_points_copied_total`) **não aparecem** no scrape de
  `openmemory-mcp:8765`, exatamente como antes desta mudança. Expor essas
  métricas exige um endpoint próprio por worker (ex.: `start_http_server`) e um
  job de scrape adicional — trabalho futuro.
- Com o worker embutido (`RUN_EMBEDDED_WORKER=true`, mesmo processo da API) as
  métricas dele aparecem normalmente.

### Aplicar

Rebuild/recreate **somente** da API (e do write worker, que compartilha a
imagem e passa a usar o novo ENTRYPOINT — inofensivo sem a variável):

```bash
cd openmemory
docker compose -f docker-compose.scale.yml build openmemory-mcp
docker compose -f docker-compose.scale.yml up -d --no-deps openmemory-mcp openmemory-write-worker
```

Nunca `down -v`; não recriar `mem0_store`.

## Verificação

```bash
docker exec openmemory_api sh -c 'echo $PROMETHEUS_MULTIPROC_DIR; ls /tmp/prometheus-multiproc'
curl -s localhost:8765/metrics | grep -E '^mcp_search_latency_seconds_count|^write_queue_depth'
curl -s localhost:8765/metrics | grep -c 'pid="'   # esperado: 0
```

Notas:

- Em modo multiprocesso **não** há `process_*`/`python_gc_*`/`python_info`
  (limitação do `prometheus_client`). Use cAdvisor/node-exporter para CPU/RAM.
- Desligar: remover `PROMETHEUS_MULTIPROC_DIR` do serviço e recriar o container
  volta ao comportamento histórico.
