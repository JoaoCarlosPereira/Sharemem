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

- **Start do container:** `docker-entrypoint.sh` (ENTRYPOINT da imagem) limpa o
  diretório antes de qualquer processo Python (alembic, uvicorn, workers). Ver
  [Escopo da limpeza](#escopo-da-limpeza-do-entrypoint).
- **Saída de processo:** `mark_process_dead` via `atexit` (registrado no import
  de `app.utils.metrics`) e no shutdown do FastAPI. Remove só os Gauges `live*`
  daquele PID; counters/histogramas permanecem para não "voltarem no tempo".
- **SIGKILL/OOM:** nenhum gancho roda; arquivos de Gauges `live*` do PID morto
  ficam até o próximo restart do container (que limpa o diretório).

### Escopo da limpeza do entrypoint

> Card b52c475a. Antes a limpeza era `find … -name '*.db' -exec rm`, o que
> apagaria `openmemory.db` se a variável apontasse para o WORKDIR.

**Remove** somente arquivos regulares de primeiro nível com o nome exato gerado
pelo `prometheus_client` (`values.MultiProcessValue`, `process_identifier`
padrão = `os.getpid()`), com `<pid>` só dígitos:

| Tipo | Arquivo |
|------|---------|
| Counter | `counter_<pid>.db` |
| Histogram | `histogram_<pid>.db` |
| Summary | `summary_<pid>.db` |
| Gauge | `gauge_<modo>_<pid>.db`, `<modo>` ∈ `all`, `liveall`, `min`, `livemin`, `max`, `livemax`, `sum`, `livesum`, `mostrecent`, `livemostrecent` |

Qualquer outro arquivo (`outro.db`, `counter_abc.db`, `gauge_123.db`,
subdiretórios) é preservado; symlinks não são seguidos nem removidos. Se uma
versão futura do `prometheus_client` mudar o formato dos nomes ou se for
configurado um `process_identifier` não numérico, os arquivos deixam de ser
limpos (falha segura: agregado pode carregar resíduos, nada é apagado por
engano) — revisar o padrão em `is_prometheus_db` ao atualizar a lib.

**Recusa** (exit `64`, mensagem `PROMETHEUS_MULTIPROC_DIR=… recusado: …`, o
comando do container não roda), antes de apagar qualquer coisa:

- diretório raiz em qualquer grafia: `/`, `//`, `/.`, `/./`, `/tmp/..`,
  caminho relativo que resolva para `/`, symlink para `/`;
- o WORKDIR da imagem, `/usr/src/openmemory` (também `…/openmemory/`,
  `//usr//src/./openmemory`, etc.);
- qualquer diretório que contenha `openmemory.db` (SQLite da API).

A normalização é léxica, em POSIX `sh` (o `python:3.12-slim` usa `dash`), sem
depender de `realpath`; depois do `mkdir -p` o caminho físico é obtido com
`cd … && pwd -P` (resolve symlinks) e as mesmas regras são reaplicadas.

**Não** recusamos diretórios que contenham outros arquivos não-Prometheus
(seria frágil: qualquer arquivo temporário derrubaria o start). Eles apenas
são ignorados. Use sempre um diretório dedicado (tmpfs próprio do serviço).

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
# Série sempre presente (histograma é criado no import, mesmo sem buscas):
curl -s localhost:8765/metrics | grep -E '^mcp_search_latency_seconds_count'
# Gauges: a linha "# TYPE" sempre aparece; a amostra só depois do 1º set().
curl -s localhost:8765/metrics | grep -E '^(# TYPE )?write_queue_depth'
curl -s localhost:8765/metrics | grep -c 'pid="'   # esperado: 0
```

No `docker-compose.scale.yml` (`RUN_EMBEDDED_WORKER=false`) a API **não**
publica `write_queue_depth`; o último comando acima retorna apenas
`# TYPE write_queue_depth gauge` (sem amostra) — isso é esperado, não falha.
Com o worker embutido, a amostra `write_queue_depth <n>` aparece após o
primeiro ciclo do loop.

### Gauges sem amostra até o primeiro `set()`

Em modo multiprocesso, Gauges `mostrecent`/`livemostrecent` **não têm amostra
no scrape enquanto nenhum processo chamou `set()`** (o `MultiProcessCollector`
só lê o que foi gravado nos arquivos; Gauges `sum`/`max`/`all` aparecem com
`0.0` por já terem sido inicializados). Sem multiprocesso eles aparecem como
`0.0` desde o import. Consequências:

- Painel **Write queue depth** (`openmemory-scale.json`, `expr:
  write_queue_depth`) mostra **No data** até o primeiro `set()` — e
  permanentemente no scrape da API quando o worker roda em outro container.
- Alerta `write_queue_depth > 100` nunca dispara sem a série (não é falso
  positivo, mas também não detecta backlog).

Se quiser o painel com linha em zero, use no Grafana:

```promql
write_queue_depth or vector(0)
```

Cuidado: `or vector(0)` também mascara "métrica ausente" como "fila vazia".
Para monitorar a fila de verdade, exponha o `/metrics` do write worker
(trabalho futuro, ver Deploy) ou alerte sobre ausência com
`absent(write_queue_depth)`. O dashboard JSON não foi alterado.

### Limite do tmpfs (64 MB)

Cada PID cria até um arquivo por tipo/modo usado (`counter_`, `histogram_`,
`gauge_<modo>_` …), cada um pré-alocado em 64 KiB (`_INITIAL_MMAP_SIZE`) e
dobrando quando enche. Medido: ~5–6 arquivos por processo (~384 KiB
aparentes; páginas efetivamente escritas são bem menos). Counters e
histogramas de PIDs mortos **não** são apagados até o próximo restart do
container, então:

- com `--workers 1` e sem reciclagem o uso é estável (poucos MB);
- se no futuro houver reciclagem frequente de workers (`--limit-max-requests`,
  crash-loop do uvicorn, gunicorn `max_requests`), cada novo PID soma arquivos
  e o tmpfs de 64 MB pode encher (~150+ PIDs no pior caso). Sintoma: `OSError:
  [Errno 28] No space left on device` ao criar métricas e `/metrics` com 500.
  Mitigação: aumentar `size=` do tmpfs, reduzir reciclagem ou reiniciar o
  container (o entrypoint limpa o diretório). Acompanhe com
  `docker exec openmemory_api du -sh /tmp/prometheus-multiproc`.

Notas:

- Em modo multiprocesso **não** há `process_*`/`python_gc_*`/`python_info`
  (limitação do `prometheus_client`). Use cAdvisor/node-exporter para CPU/RAM.
- Desligar: remover `PROMETHEUS_MULTIPROC_DIR` do serviço e recriar o container
  volta ao comportamento histórico.
