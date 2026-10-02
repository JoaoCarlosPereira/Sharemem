# Runbook — Calibrar `MEM0_AUTODEDUP_THRESHOLD` com o relatório do modo `report`

> Card ddbc19a9. Pré-requisito para sequer **considerar** `MEM0_AUTODEDUP_MODE=apply`.

## O que existe

Com `MEM0_AUTODEDUP_MODE=report` (padrão do `docker-compose.scale.yml`), o
write-worker, após cada escrita, busca no Qdrant os vizinhos de cada memória
recém-extraída e **apenas registra** os pares parecidos. Nada é marcado obsoleto.

Cada par vira uma linha na tabela PostgreSQL **`autodedup_reports`** (migration
`s1t2u3v4w5x6`), além do `logger.info` de sempre:

| Coluna | Significado |
|--------|-------------|
| `created_at` | Quando o par foi visto (UTC) — só o modo `report` grava aqui |
| `job_id`, `project` | Job da `write_queue` e projeto da memória **nova** |
| `new_memory_id` / `duplicate_memory_id` | Memória nova e a existente parecida (a que `apply` marcaria obsoleta) |
| `duplicate_project` | Projeto da memória existente (útil para ver pares entre projetos) |
| `score` | Similaridade de cosseno retornada pelo Qdrant |
| `threshold` | `MEM0_AUTODEDUP_THRESHOLD` vigente no momento |
| `above_threshold` | `true` = `apply` superseria hoje; `false` = "quase" (abaixo do limiar) |
| `new_text` / `duplicate_text` | Trechos **curtos** (160 caracteres por padrão) para julgar o par |

### "Quase duplicatas" (abaixo do limiar)

Para calibrar **abaixo** de 0.95 o modo report também grava pares com
`MEM0_AUTODEDUP_REPORT_FLOOR <= score < MEM0_AUTODEDUP_THRESHOLD`
(`above_threshold=false`). É a mesma busca já feita por item; só o corte de
gravação é mais baixo. O modo `apply` ignora o piso e continua usando apenas o
limiar — o comportamento de `off`/`apply` não mudou.

### Variáveis (todas opcionais; via `api/.env`)

| Variável | Padrão | Efeito |
|----------|--------|--------|
| `MEM0_AUTODEDUP_REPORT_FLOOR` | `0.85` | Menor score gravado no modo report (limitado a `[0, threshold]`; inválido/`nan` → `0.85` com WARNING) |
| `MEM0_AUTODEDUP_REPORT_TEXT_CHARS` | `160` | Tamanho máximo dos trechos; `0` = não grava texto |
| `MEM0_AUTODEDUP_REPORT_RETENTION_DAYS` | `30` | Remove linhas mais antigas que N dias; `0` = sem limite por idade |
| `MEM0_AUTODEDUP_REPORT_MAX_ROWS` | `50000` | Mantém só as N linhas mais recentes; `0` = sem teto |

`MEM0_AUTODEDUP_THRESHOLD` precisa estar em `[0, 1]`; `nan`, `inf`, fora da faixa
ou texto inválido voltam para `0.95` com WARNING (antes, `nan` desligava o
`apply` em silêncio, porque `score >= nan` é sempre falso).

**Retenção:** aplicada pelo próprio write-worker logo após gravar, no máximo uma
vez a cada 5 min por processo, e **somente** em `autodedup_reports` (nunca em
`write_queue`, `write_audit_logs` ou no Qdrant). Falhas de gravação ou de
retenção viram `WARNING` no log e **não** afetam o job de escrita.

**Privacidade:** apenas trechos truncados; o endpoint exige admin. Se preferir
não ter texto nenhum no PostgreSQL, use `MEM0_AUTODEDUP_REPORT_TEXT_CHARS=0` e
consulte os textos pelos IDs na UI.

## Deploy

1. `alembic upgrade head` (já é feito no start do `openmemory-mcp`; migration aditiva — só cria a tabela).
2. Rebuild/recreate **somente** `openmemory-mcp` e `openmemory-write-worker`.
   Nada de `down -v`, nada de mexer em `mem0_store`.

Se o worker subir antes da migration, a gravação do relatório falha com WARNING
e o job segue normalmente; ao aplicar a migration, passa a gravar.

Quem aplicou a versão de **desenvolvimento** desta migration (tabela com coluna
`mode NOT NULL`) não precisa fazer nada: o `upgrade` detecta schema incompatível
(falta coluna esperada ou sobra coluna `NOT NULL` sem default) e **recria só
`autodedup_reports`** (dados de relatório, descartáveis). Nenhuma outra tabela é
tocada. Mas como a revisão é a mesma (`s1t2u3v4w5x6`), um banco já marcado nela
não roda o `upgrade` de novo; nesse caso:
`alembic downgrade r0s1t2u3v4w5 && alembic upgrade head` (o downgrade também só
remove `autodedup_reports`).

## Consultar

```bash
API=http://192.168.3.213:8765          # ou a URL interna da API
H="X-Admin-Token: $ADMIN_TOKEN"

# Visão geral (últimos 100 pares por score + resumo de TODOS os filtrados)
curl -s -H "$H" "$API/admin/autodedup/report" | jq '.config, .summary'

# Desde uma data, só um projeto, só score >= 0.90, até 500 itens
curl -s -H "$H" "$API/admin/autodedup/report?since=2026-10-03T00:00:00Z&project=sysmovs&min_score=0.90&limit=500" | jq

# Pares de uma faixa para revisar à mão (ex.: [0.92, 0.95))
curl -s -H "$H" "$API/admin/autodedup/report?min_score=0.92&max_score=0.95&limit=1000" \
  | jq -r '.items[] | [.score, .project, .new_text, .duplicate_text] | @tsv'

# Só as quase-duplicatas (abaixo do limiar vigente na gravação)
curl -s -H "$H" "$API/admin/autodedup/report?above_threshold=false&limit=500" | jq '.items'
```

Parâmetros: `since` (ISO 8601; sem fuso = UTC), `min_score` (`>=`, 0–1),
`max_score` (`<`, 0–1), `above_threshold` (`true`/`false`, conforme o limiar
vigente **quando o par foi gravado**), `project` (projeto da memória nova),
`limit` (0–1000, só limita `items`). O `summary` sempre agrega **todas** as
linhas filtradas e é calculado no banco (uma consulta agregada, sem carregar
linhas), então continua barato mesmo com `MAX_ROWS=0`/`RETENTION_DAYS=0`.
Sem credencial admin → `401`.

## Ler o resumo

```json
"summary": {
  "total_pairs": 412,
  "below_histogram": 0,
  "report_floor": 0.85,
  "histogram": [{"min": 0.85, "max": 0.86, "count": 37, "below_report_floor": false}, ..., {"min": 0.99, "max": 1.0, "count": 9, "below_report_floor": false}],
  "thresholds": [
    {"threshold": 0.85, "pairs": 412, "would_supersede": 300, "new_memories": 280, "current": false, "below_report_floor": false},
    ...
    {"threshold": 0.95, "pairs": 41, "would_supersede": 35, "new_memories": 33, "current": true, "below_report_floor": false},
    ...
  ]
}
```

- **`histogram`**: contagem de pares por faixa de 0.01 entre 0.85 e 1.00
  (`[min, max)`, a última inclui 1.0). Duplicatas reais tendem a se concentrar no
  topo; pares de assuntos diferentes formam a "cauda" mais baixa. Procure o
  **vale** entre as duas massas.
- **`thresholds`**: para cada limiar candidato (0.85…0.99 + o atual, sem
  arredondar), quantos pares teriam `score >= limiar` (`pairs`), quantas memórias
  existentes distintas `apply` marcaria obsoletas (`would_supersede`) e quantas
  memórias novas as causariam (`new_memories`). `current: true` marca o limiar
  vigente. A comparação é **exata**, igual à do `apply` (sem tolerância): um
  score `0.94999998` (float32 do Qdrant) não conta em `0.95`, nem lá nem aqui.
- **`below_report_floor: true`** (em `thresholds` e `histogram`): o limiar/faixa
  está abaixo de `report_floor`. O modo report **não grava** pares abaixo do
  piso, então esses números são apenas um mínimo (só contam pares gravados
  quando o piso era mais baixo). **Não** use essas linhas para decidir; para
  avaliar limiares menores, baixe `MEM0_AUTODEDUP_REPORT_FLOOR` e espere tráfego.

## Medir grupos de duplicatas JÁ existentes (somente leitura)

O relatório só registra pares **no momento de uma escrita nova**. Os grupos
reais conhecidos já estão no Qdrant e **não aparecem** em `items` — nenhum
`jq` vai encontrá-los. Para eles use o script
`openmemory/scripts/autodedup-calibrate-groups.py`, que:

- embute o texto de cada membro com o **mesmo embedder e a mesma chamada** do
  autodedup (`embed(texto, "search")`) e consulta o Qdrant (`query_points` /
  `retrieve`), logo o score é o mesmo cosseno que o autodedup veria;
- **não escreve nada**: nem Qdrant (só `query_points`/`retrieve`; o
  `Qdrant.__init__` do mem0 nem é chamado, porque ele cria coleção/índices),
  nem SQL (só um `SELECT` em `configs` para descobrir o embedder), nem
  `autodedup_reports`. Não chama `add`/`update`/`delete`.

Passo a passo:

1. Copie `docs/runbooks/autodedup-calibration-groups.example.yaml` e ajuste
   (cada grupo = memórias que afirmam **a mesma coisa**; `ids` e/ou `query` +
   `project` + `top`).
2. Rode **no container** do write-worker (mesma config/embedder que escreve):

   ```bash
   cd openmemory
   docker compose -f docker-compose.scale.yml cp grupos.yaml openmemory-write-worker:/tmp/grupos.yaml
   # confere o arquivo (sem rede, sem banco):
   docker compose -f docker-compose.scale.yml exec -T openmemory-write-worker \
     python /usr/src/openmemory/scripts/autodedup-calibrate-groups.py /tmp/grupos.yaml --validate-only
   # mede:
   docker compose -f docker-compose.scale.yml exec -T openmemory-write-worker \
     python /usr/src/openmemory/scripts/autodedup-calibrate-groups.py /tmp/grupos.yaml \
     --margin 0.01 > calibracao.json
   ```

   Na imagem (`openmemory/api/Dockerfile`) a API fica em `/usr/src/openmemory`
   (`app/` direto lá, `WORKDIR`) e os scripts em `/usr/src/openmemory/scripts`;
   o fork `mem0` vem por `PYTHONPATH=/usr/src`. O script localiza sozinho o
   diretório que contém `app/` (`/usr/src/openmemory` na imagem,
   `openmemory/api` num checkout), então funciona chamado por caminho absoluto
   a partir de qualquer cwd. `-T` (sem TTY) evita misturar stderr no JSON
   redirecionado. Num checkout local (fora do container), o equivalente é
   `python openmemory/scripts/autodedup-calibrate-groups.py grupos.yaml` com a
   venv da API e as mesmas variáveis de ambiente do worker.

   (Leitura apenas — não reinicia nem recria serviço; `mem0_store` intocado.)

   **Embedder Ollama:** o `OllamaEmbedding` do mem0 faz `pull` do modelo quando
   ele não está no servidor Ollama. O script **não deixa isso acontecer**: antes
   de construir o embedder ele consulta `client.list()` (somente leitura) e, se
   o modelo configurado faltar, aborta com
   `modelo de embedding Ollama '<modelo>' ausente ... o script não baixa modelos`.
   Nesse caso o próprio write-worker também estaria sem o modelo — corrija a
   config/servidor Ollama antes; não rode `ollama pull` só para calibrar sem
   combinar com quem opera o servidor.
3. **Confira `members`** (id + texto de cada membro). Queries podem trazer
   memória errada; se trouxerem, fixe o grupo por `ids` e rode de novo. Leia
   `warnings` (ids ausentes, quarentenados, grupos com < 2 membros).
4. Leia o resultado:
   - `pairs`: cada par com `score_a_new`/`score_b_new` (os dois sentidos). Em
     pares **intra**-grupo vale o **menor** sentido (pior caso para capturar);
     em pares **entre** grupos, o **maior** (pior caso de falso positivo).
   - `groups.matrix`: diagonal = menor score intra do grupo; fora da diagonal =
     maior score entre os dois grupos (ex.: `fcr722` x `sicredi-trgn`).
   - `calibration`: `min_intra`, `max_inter`, `gap`, `interval` =
     `[max_inter + margem, min_intra]`, `recommended` (topo do intervalo
     arredondado para baixo em 0.01) e `current_ok` (o limiar atual cabe no
     intervalo?).
   - `outside_neighbors` / `max_outside_neighbor`: vizinhos top-k de cada membro
     que **não** estão em grupo nenhum — não rotulados; revise à mão os que
     ficarem dentro do intervalo (podem ser duplicata não listada ou falso
     positivo).

## Critério para escolher o limiar

O objetivo é **zero falso positivo entre assuntos diferentes**: em `apply`, um
falso positivo esconde (marca obsoleta) uma memória correta. Perder uma
duplicata custa pouco; apagar um fato distinto custa caro.

1. **Espere tráfego suficiente** (alguns dias; idealmente ≥ 100 pares acima de 0.90).
2. **Meça os grupos reais conhecidos com o script** (seção anterior — eles não
   estão em `items`):
   - 3 memórias "Sicredi 748/BB/Itau/Banrisul usam TRgnFinanceiroBoletoHibrido";
   - 2 sobre "PATCH /boletos/{nossoNumero} retorna 204";
   - 2 sobre "Fin104 persiste em GCVPRM02.JSON_DADOS_API";
   - 3 sobre "Cobranca bancaria na tela Fcr722".
   Pares **intra**-grupo devem cair **acima** do limiar; pares **entre** grupos
   (ex.: Fcr722 x TRgnFinanceiroBoletoHibrido, ambos de cobrança/boletos) são
   assuntos vizinhos porém diferentes e **devem ficar abaixo** — esse é o falso
   positivo mais provável. Critério do gabarito:
   - `calibration.interval` não nulo ⇒ todo L nesse intervalo separa o
     gabarito com a margem pedida (`max_inter + margem <= L <= min_intra`);
   - `interval` nulo (`separable: false` ou folga menor que a margem) ⇒ **não
     existe limiar seguro** para esses assuntos: fique em `report`;
   - o L final é o **maior** entre o limite inferior do gabarito
     (`interval.min`) e o exigido pelo tráfego real (passos 3–4). Se esse valor
     passar de `interval.max` (`min_intra`), aceite perder as duplicatas do
     gabarito — **nunca** baixe o limiar por elas.
   Pares novos que envolvam esses assuntos também aparecem em `items` a partir
   do deploy (ex.: `jq '.items[] | select(.new_text|test("Fcr722"))'`).
3. **Revise à mão as faixas próximas do candidato**: para o limiar L, leia todos
   os pares em `[L, L+0.02)`. Se **qualquer** par for de assuntos diferentes
   (fatos distintos, bancos/endpoints/tabelas diferentes, números diferentes),
   suba o limiar acima do score desse par.
4. **Escolha o menor L** em que (a) todos os pares `>= L` revisados são
   duplicatas reais e (b) o score do falso positivo mais alto encontrado fica
   **pelo menos 0.01 abaixo** de L (margem). Se os grupos-gabarito ficarem
   abaixo desse L, aceite perdê-los — não baixe o limiar por eles.
   Nunca escolha L em linha com `below_report_floor: true`.
5. Registre a decisão (limiar, data, nº de pares revisados, maior falso positivo,
   `min_intra`/`max_inter`/`interval` do script e o arquivo de grupos usado)
   no card antes de qualquer mudança para `apply`. Mudar para `apply` é decisão
   separada, com pedido explícito.

Fatos que diferem só em um identificador (banco 748 x 001, `PATCH` x `POST`,
tela Fcr722 x Fcr723) costumam ter cosseno muito alto: trate-os como **falso
positivo** mesmo acima de 0.95 — se aparecerem, o limiar precisa subir ou o
`apply` não é seguro para esse domínio.
