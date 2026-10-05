# Runbook — Rerank local (cross-encoder em CPU)

Card 80110071. **Desligado por padrão.** Sem `MEM0_RERANKER_PROVIDER` nada é
importado nem carregado, e `search_memory(rerank=true)` responde
`rerank: {applied: false, reason: "not_configured"}`. Ligar em produção é decisão
do operador — siga este passo a passo e meça antes/depois.

O rerank é **local**: um cross-encoder pequeno (sentence-transformers) roda em CPU
dentro do container da API. Nenhum conteúdo de memória sai da LAN. Providers
remotos (`cohere`, `zero_entropy`, `llm_reranker`) são **recusados** quando
`MEM0_LOCAL_ONLY=1` (`reason: blocked_local_only: ...`). LLM no Ollama como
reranker foi descartado: lento demais para o caminho de leitura.

## Como funciona

1. A busca recupera o pool de candidatos (`MEM0_SEARCH_CANDIDATE_K`, 150) e o
   ordena como sempre: `score semântico × recência × projeto × grupo × lexical`.
2. Só com `rerank=true` na chamada: os **`MEM0_RERANKER_TOP_N` melhores** dessa
   ordenação (default 30, nunca menos que a página de 20) vão ao cross-encoder.
   Selecionar *depois* do blend preserva o resgate por recência/grupo que motivou
   o pool largo.
3. Os scores do cross-encoder (logits sem limite) são normalizados sobre esse topo
   para `[0,05; 1]` (`0,05 + 0,95 × minmax` — o último do topo não vira 0, senão
   nenhum boost conseguiria levantá-lo) e passam a ser o `score`; o topo é
   reordenado com os **mesmos** boosts. Logo, para item rerankeado:

   ```text
   effective_score = rerank_norm × recency × project × group × lexical
   ```

   O resto do pool (cauda) fica depois do topo, na ordem original — o modelo não
   o avaliou, então o cosseno dele não é comparável. Como `TOP_N ≥ 20`, a página
   devolvida é 100% rerankeada.
4. Cada item rerankeado traz `rerank_score` (bruto), `semantic_score` (cosseno
   original), `score` (normalizado), `effective_score` e `ranking_factors`.

### Fallbacks (a busca nunca quebra por causa do rerank)

| Situação | Resposta `rerank` | Ordem |
|----------|-------------------|-------|
| Provider não configurado | `applied=false`, `reason=not_configured` | original |
| Modelo ainda carregando | `applied=false`, `reason=loading` | original |
| Pacote ausente / modelo inválido | `applied=false`, `reason=unavailable: ...` | original |
| Exceção no rerank | `applied=false`, `reason=failed: ...` | original |
| Modelo fora do cache local (runtime offline) | `applied=false`, `reason=unavailable: modelo 'X' não está no cache local ...; rebuild com RERANK_PRELOAD_MODEL=X` | original |
| Estourou `MEM0_RERANKER_TIMEOUT_SEC` | `applied=false`, `reason=timeout: ...` | original |
| Todas as vagas ocupadas (inclusive por passe abandonado após timeout) | `applied=false`, `reason=busy` — **na hora**, sem fila | original |
| Circuit breaker aberto | `applied=false`, `reason=circuit_open` — na hora, modelo não é chamado | original |
| Sucesso | `applied=true`, `provider`, `model`, `reranked`, `candidates`, `latency_ms` | rerankeada |

O modelo **nunca** é carregado no caminho da requisição: com
`MEM0_RERANKER_WARMUP=1` (default) carrega numa thread no startup da API; com `0`
a primeira busca com `rerank=true` dispara a carga em background e responde
`loading`.

## Variáveis (`openmemory/api/.env`)

| Variável | Default | Significado |
|----------|---------|-------------|
| `MEM0_RERANKER_PROVIDER` | vazio (desligado) | `sentence_transformer` (recomendado) ou `huggingface` |
| `MEM0_RERANKER_MODEL` | default do SDK | id HF do cross-encoder (ver abaixo) |
| `MEM0_RERANKER_DEVICE` | `cpu` | device torch |
| `MEM0_RERANKER_BATCH_SIZE` | `16` | lote de pares (consulta, memória) |
| `MEM0_RERANKER_TOP_N` | `30` | candidatos re-pontuados (mín. 20) |
| `MEM0_RERANKER_TIMEOUT_SEC` | `3` | orçamento por chamada; estourou → ordem original |
| `MEM0_RERANKER_MAX_CONCURRENCY` | `1` | passes de rerank simultâneos; o excedente responde `busy` na hora (não enfileira) |
| `MEM0_RERANKER_THREADS` | `2` | threads do torch (`torch.set_num_threads`), limitado a `[1, nº de CPUs]`; também vira default de `OMP_NUM_THREADS`/`MKL_NUM_THREADS` se não definidos |
| `MEM0_RERANKER_BREAKER_THRESHOLD` | `3` | timeouts **consecutivos** que abrem o circuit breaker |
| `MEM0_RERANKER_BREAKER_COOLDOWN_SEC` | `60` | tempo aberto (`circuit_open`); depois 1 sonda (half-open): sucesso fecha, timeout/falha reabre |
| `MEM0_RERANKER_ALLOW_DOWNLOAD` | `0` | `1` libera o loader a falar com huggingface.co (só dev; **ignorado** com `MEM0_LOCAL_ONLY=1`) |
| `MEM0_RERANKER_WARMUP` | `1` | pré-carregar no startup |
| `MEM0_RERANKER_MAX_LENGTH` | `512` | só provider `huggingface` |

Build args (`docker-compose.scale.yml` → `x-api-common.build.args`, lidos do
`.env` do compose ou do shell):

| Build arg | Default | Significado |
|-----------|---------|-------------|
| `INSTALL_RERANK` | `0` | `1` instala `api/requirements-rerank-torch.txt` (torch CPU, passo próprio com `--index-url https://download.pytorch.org/whl/cpu` — nunca o wheel CUDA) e depois `api/requirements-rerank.txt` (sentence-transformers); ~+1,8 GB (medido: 885 MB → 2,71 GB) |
| `RERANK_PRELOAD_MODEL` | vazio | baixa o modelo **no build** para `HF_HOME=/opt/hf-cache` dentro da imagem |

### Rede: runtime sempre offline

O loader roda o stack Hugging Face **offline por padrão** (decisão do card, vale
com ou sem `MEM0_LOCAL_ONLY`): antes de importar torch/transformers ele força
`HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1` e `HF_HUB_DISABLE_TELEMETRY=1`.
Com `MEM0_LOCAL_ONLY=1` o `main.py` também os define antes de qualquer import do
app (como já fazia com `MEM0_TELEMETRY`). Sem isso, carregar o `CrossEncoder`
consultava o hub a cada startup e baixava o modelo se ele não fosse o do preload.

> **Escopo: o processo inteiro da API.** As variáveis acima são de ambiente do
> processo, não só do reranker. Com `MEM0_RERANKER_PROVIDER` configurado, **todo**
> o uvicorn fica offline para o Hugging Face — um embedder `huggingface` ou o
> modelo BM25 do fastembed também passam a exigir o modelo já no cache local
> (senão falham em vez de baixar). Confira isso antes de ligar o rerank num
> deploy que use esses componentes.
>
> **Ordem de import importa.** O loader só corrige em runtime a constante do
> `huggingface_hub` já importado; o `transformers` guarda o próprio flag offline
> no import e **não** é corrigido depois. Se algo importar `transformers` antes
> do loader, ele continua com o valor antigo. Por isso, com `MEM0_LOCAL_ONLY=1`,
> o `main.py` define as variáveis antes de qualquer import do app.

Consequência: **sem `RERANK_PRELOAD_MODEL` o rerank não funciona** — responde
`unavailable: modelo 'X' não está no cache local (...); rebuild com
RERANK_PRELOAD_MODEL=X` e a busca segue normal. O `MEM0_RERANKER_MODEL` do `.env`
tem de ser **o mesmo** do build (sem `MEM0_RERANKER_MODEL`, vale o default do SDK,
`cross-encoder/ms-marco-MiniLM-L-6-v2`). O download só acontece no `RUN` de
preload do Dockerfile (único lugar com `HF_HUB_OFFLINE=0`).

`MEM0_RERANKER_ALLOW_DOWNLOAD=1` é o opt-out explícito para dev fora do
local-only (baixa para `HF_HOME` e se perde ao recriar o container). **Não** crie
volume novo para o cache HF e **não** altere os volumes existentes.

### Escolha do modelo

O acervo é majoritariamente PT-BR. Teste local (CPU 24 threads, 51 memórias de
~200 tokens, a memória-alvo inserida no meio do pool):

| Modelo | Carga | rerank 51 docs p50 | Alvo em 1º (5 consultas PT-BR) |
|--------|-------|--------------------|--------------------------------|
| `cross-encoder/ms-marco-MiniLM-L-6-v2` (inglês) | ~9 s | ~500 ms | 1/5 |
| `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1` (multilíngue) | ~11 s | ~600 ms | 3/5 |

Com `TOP_N=20` o ms-marco caiu para ~190 ms p50. O custo é ~linear em `TOP_N` e
no tamanho das memórias — **meça no servidor** antes de decidir. Recomendação
inicial: `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1`, `TOP_N=30`, timeout 3 s.
`BAAI/bge-reranker-base` (multilíngue, ~280 M parâmetros) é mais preciso mas
bem mais lento em CPU; só com medição.

Medição do QA no container (imagem `INSTALL_RERANK=1`, bench abaixo):
`cross-encoder/ms-marco-MiniLM-L-6-v2`, 20 candidatos, `MEM0_RERANKER_THREADS=2`,
CPU de 24 núcleos → etapa de rerank **p50 ~300 ms, p95 ~337 ms**; carga do modelo
**~9 s** no container. Bem abaixo do timeout de 3 s, mas some isso ao `total_sem`.

## Ativação (passo a passo)

> Regras CRITICAL do repo valem aqui: rebuild **somente** dos serviços da API;
> nunca `down -v`, nunca recriar `mem0_store`. Confira `points_count` antes e
> depois: `curl -s localhost:6333/collections/openmemory | jq .result.points_count`.

1. **Medir o "antes"** (seção abaixo) com a imagem atual.
2. Build da imagem com rerank (o preload é **obrigatório**, ver "Rede"):

   ```bash
   cd openmemory
   INSTALL_RERANK=1 \
   RERANK_PRELOAD_MODEL=cross-encoder/mmarco-mMiniLMv2-L12-H384-v1 \
     docker compose -f docker-compose.scale.yml build openmemory-mcp
   ```

   `scripts/rebuild-ui-api.sh` também repassa `INSTALL_RERANK` /
   `RERANK_PRELOAD_MODEL` do ambiente — exporte-os se usar o script, senão o
   rebuild volta a uma imagem sem torch (o rerank passa a responder
   `unavailable`, a busca segue normal).
3. Em `openmemory/api/.env` (modelo = o mesmo do preload):

   ```bash
   MEM0_RERANKER_PROVIDER=sentence_transformer
   MEM0_RERANKER_MODEL=cross-encoder/mmarco-mMiniLMv2-L12-H384-v1
   MEM0_RERANKER_TOP_N=30
   MEM0_RERANKER_TIMEOUT_SEC=3
   MEM0_RERANKER_THREADS=2
   ```

4. Recriar **só** a API: `docker compose -f docker-compose.scale.yml up -d --no-deps openmemory-mcp`.
   (Os workers usam a mesma imagem mas não servem busca; não precisam do rerank.)
5. Conferir:
   - `curl localhost:8765/admin/rerank` → `configured=true`, `reason=null`
     (ou `loading` nos primeiros segundos), `model`, `device=cpu`, `top_n`,
     `threads`, `offline=true` e `breaker` (`state=closed`, `inflight`,
     `abandoned_inflight`, `consecutive_timeouts`, `opened_total`).
   - `curl localhost:8765/health` → `checks.rerank` idem (informativo, nunca derruba health).
   - Uma busca MCP com `rerank=true` → `rerank.applied=true`.
6. **Medir o "depois"** e comparar.

### Desligar / rollback

Remova `MEM0_RERANKER_PROVIDER` do `.env` e recrie só `openmemory-mcp`. Não é
preciso rebuild: sem o provider a imagem com torch se comporta como a padrão
(só ocupa mais disco/RAM de imagem; o modelo não é carregado).

## Medir p95 antes/depois

Mesmo método do PR #28 (calibragem do `MEM0_SEARCH_CANDIDATE_K`): **dentro do
container da API**, contra o Qdrant real, **consultas distintas**, **sem cache**.
O script `scripts/bench-rerank-latency.py` é somente leitura: embeda cada consulta
(não usa o read cache Redis), busca o pool no Qdrant, ranqueia e — se o provider
estiver configurado — rerankeia, cronometrando cada etapa. Não chama
`search_memory`, não grava nada.

O script roda **por caminho absoluto** dentro do container: ele próprio acha o
diretório com `app/` (`/usr/src/openmemory` na imagem, `openmemory/api` no
checkout), sem depender do cwd nem de `PYTHONPATH` extra. `--help` não importa o
app; `--check-imports` só confere os imports (sem rede, Qdrant, banco nem torch)
— use-o para validar a imagem antes de medir. `-T` desliga o TTY: obrigatório ao
redirecionar a saída (`> arquivo`), senão stderr se mistura ao arquivo no host.

```bash
cd openmemory
# Sanidade da imagem (não toca Qdrant/banco):
docker compose -f docker-compose.scale.yml exec -T openmemory-mcp \
  python /usr/src/openmemory/scripts/bench-rerank-latency.py --check-imports

# ANTES (imagem/env atuais, sem provider): baseline embed + qdrant + rank
docker compose -f docker-compose.scale.yml exec -T openmemory-mcp \
  python /usr/src/openmemory/scripts/bench-rerank-latency.py --rounds 3 --json > bench-antes.json

# DEPOIS (imagem com INSTALL_RERANK=1): o mesmo, com o provider ligado.
# Pode variar modelo/TOP_N por -e sem tocar no .env nem recriar o container:
docker compose -f docker-compose.scale.yml exec -T \
  -e MEM0_RERANKER_PROVIDER=sentence_transformer \
  -e MEM0_RERANKER_MODEL=cross-encoder/mmarco-mMiniLMv2-L12-H384-v1 \
  -e MEM0_RERANKER_TOP_N=30 \
  openmemory-mcp python /usr/src/openmemory/scripts/bench-rerank-latency.py --rounds 3 --json > bench-depois.json
```

Limitações do bench:

- **Boost de grupo:** por padrão ranqueia sem grupo do solicitante (fator 1,0
  para todos), então não reproduz o resgate por grupo da busca real. Use
  `--group NOME` ou `--owner HOSTNAME` (resolve o grupo só lendo — não cadastra
  o usuário).
- **RAM:** com provider ligado o script carrega uma **segunda cópia** do modelo
  no mesmo container (além da do uvicorn): pico de ~+1 modelo (centenas de MB)
  durante a medição. Rode fora do horário de pico e confira `docker stats`.

Saída: `p50/p95/max` por etapa (`embed`, `qdrant`, `rank`, `rerank`) e o total
`total_sem` vs `total_com`. `--queries arquivo.txt` usa consultas reais (uma por
linha; prefira 20+ distintas tiradas do `read_audit_logs`/uso real). `--json` para
anexar ao card. Registre no card: host, data, modelo, `TOP_N`, número de consultas
e a tabela — como no comentário de `DEFAULT_SEARCH_CANDIDATE_K` em `mcp_server.py`.

Critério sugerido: aceitar se `p95(rerank) < MEM0_RERANKER_TIMEOUT_SEC` com folga
(≤ 50%) e o ganho de qualidade justificar o acréscimo sobre `total_sem`
(referência PR #28: busca ~110 ms p50 sem rerank).

Em produção, acompanhe também pelo Prometheus (`/metrics`):

- `mcp_rerank_latency_seconds` — histograma da etapa de rerank;
- `mcp_rerank_total{outcome=applied|timeout|failed|busy|circuit_open|loading|unavailable}`;
- `mcp_search_latency_seconds` — latência total do `search_memory`.

### Proteção contra CPU lenta (timeout, busy, breaker)

O torch não é interrompível: um `predict` que estoura o timeout **continua
rodando** até terminar. Por isso:

- a vaga só é liberada quando o modelo realmente devolve; enquanto isso, novas
  buscas recebem `busy` **imediatamente** (antes ficavam na fila do executor e
  estouravam o timeout também — cascata de +3 s em toda busca);
- após `MEM0_RERANKER_BREAKER_THRESHOLD` timeouts seguidos o breaker abre por
  `MEM0_RERANKER_BREAKER_COOLDOWN_SEC` (`circuit_open`, modelo não é chamado);
  depois deixa passar **uma** sonda: sucesso fecha, timeout reabre;
- `MEM0_RERANKER_THREADS` (default 2) limita o torch para não disputar CPU com
  llama.cpp/Qdrant. `OMP_NUM_THREADS`/`MKL_NUM_THREADS` só valem se definidos
  antes do primeiro `import torch`; o loader os preenche com o mesmo valor se
  ausentes (valor explícito do operador prevalece).

Leitura das métricas: muitos `timeout`/`circuit_open` = `TOP_N` alto demais para a
CPU (ou threads de menos). Muitos `busy` = mais buscas com `rerank=true` em
paralelo do que `MEM0_RERANKER_MAX_CONCURRENCY` — cada vaga extra é ~1 núcleo ×
`THREADS`. Reduza `TOP_N` antes de subir o timeout.
