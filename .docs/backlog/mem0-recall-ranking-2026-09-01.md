# mem0 — recall, ranking e higiene de escrita (01/09/2026)

Diagnóstico e correções a partir da falha de recall da tarefa 371145. Tudo que
está medido aqui saiu do acervo real (12.725 memórias, servidor 192.168.3.213).

## Resumo

| # | Problema | Estado |
|---|---|---|
| 1 | Recall do hook em prompt curto | **corrigido** |
| 4a | Backslash corrompido na gravação | **corrigido** + 11 memórias antigas listadas |
| 2 | Ranking dominado por recência | **corrigido** — mas não como se supunha (ver abaixo) |
| 3 | `project` fragmentado, sem chave de tarefa | proposta + script, **aguardando aprovação** |
| 4b | `supersedes` não é usado | proposta |
| 4c | Ruído no corpo das memórias | proposta |

---

## Problema 1 — recall do hook em prompt curto

### Diagnóstico

`mem0-hook.mjs` montava a query com o texto cru do prompt e mandava direto:

```js
body: JSON.stringify({ query: prompt, user_id: hostname(), project, limit: LIMIT })
```

Com `/tarefa 371145` a query é literalmente `"/tarefa 371145"`. Reproduzido
contra o servidor — as 5 memórias devolvidas, nenhuma da 371145:

```
score=0.781 [sysmo-s1]  TAREFA #372244 was logged in Redmine with project 131...
score=0.716 [373963]    O cliente associado à tarefa #373963 é 105307 (CAITA...
score=0.730 [sysmo-s1]  A TAREFA #368706, que estava em status Conclusão...
score=0.722 [sysmo-s1]  Em 29/06/2026, a Tarefa #363275 foi entregue no caminho...
score=0.720 [sysmo-s1]  A Tarefa #363944 foi desenvolvida para o Cliente 22321...
```

Ou seja: **0/5 relevantes**. O embedding trata "371145" como um número qualquer;
o que ele casa é o formato da frase ("tarefa X no Redmine"), não a tarefa.

### Correção

`mem0-hook.mjs` reescrito:

1. Extrai códigos de tarefa do prompt (6 dígitos, com ou sem `#`, sem dígito
   colado — `2.89.24` e `9443` não contam).
2. Havendo código: busca dedicada com query enriquecida
   (`TAREFA #371145 371145 <texto livre>`), pool de 60 candidatos, e **filtro por
   ocorrência literal do código** no texto ou no projeto. Esses resultados entram
   **antes** dos semânticos.
3. Prompt sem texto livre depois de tirar o slash-command e os códigos
   (`isLowSignal`) — a busca semântica genérica é **suprimida**. Injetar 5
   memórias irrelevantes custa contexto e induz erro; injetar nada custa nada.
4. Teto total inalterado: continua `LIMIT = 5`, com dedupe entre as duas buscas.

Depois, mesmo prompt `/tarefa 371145`, mesmo servidor:

```
- A nota original de alinhamento da TAREFA #371145 estava em .docs/nota-alinhamento-pricing.md... [sysmo-api-tributacao]
- Tarefa #371145 foi registrada em 19/08/2026 para definir alíquotas efetivas... [sysmo-s1]
```

**2/2 relevantes** (o pool só continha esses dois com o código literal — o resto
depende da correção do problema 2).

### Testes

`C:\Users\s258\.claude\mem0\mem0-hook.test.mjs`, 8 casos, `node --test`. Todos
passam. Falham contra a versão anterior: ela não expunha nenhuma dessas funções.

---

## Problema 4a — backslash corrompido na gravação

### Diagnóstico

A memória `a62a97a9-66e6-4ddc-b7bc-e471c168d12d` foi lida crua do servidor. Dump
dos caracteres:

```
... configurado é  \  1 9 2 . 1 6 8 . 3 . 5  0x09  a r e f a s  0x09  i m e  0x131  q u i p e ...
... l a t e s t  0x00  0 0 0 0 0 _ 0 0 0 0 _ m i g r a t i o n . x m l
```

TAB (0x09), NUL (0x00) e U+0131 gravados como caracteres reais — o produto de
`\t`, `\0` e um escape mal resolvido.

Onde acontece: **não** no hook nem na serialização MCP. O texto do prompt chega
íntegro; quem desmonta o caminho é a **extração**. Em `mem0/memory/main.py` a
resposta do LLM é desserializada com `json.loads(response, strict=False)`
(linhas 1089/1092) e o modelo emite o caminho Windows dentro da string JSON sem
duplicar as barras. Confirmado por outras memórias com a mesma assinatura, como a
`b3ebf514` (`...Fiscal\371227<TAB>ext{:} latest...` — o `\t` virou TAB e o modelo
ainda enfiou um `\text{}` de LaTeX). Não é bug de um provider específico: é o
caminho de extração inteiro, sem defesa nenhuma contra isso.

### Correção

`mem0/memory/technical_content.py` ganhou `repair_escape_damage(text, source_text)`:

- caractere de controle C0 numa frase extraída é a assinatura do dano (TAB
  incluso; `\n`/`\r` de fora, são separadores legítimos);
- havendo dano, os caminhos Windows/UNC íntegros são localizados no **texto de
  origem** e reenxertados verbatim sobre o trecho destruído;
- o que sobrar de controle vira uma barra invertida entre caracteres colados (a
  origem quase certa) ou é removido — nada de controle chega ao acervo.

A regex de caminho aceita espaço nos segmentos intermediários (os shares da
equipe se chamam `Equipe Financeiro Fiscal`) mas não no último, senão o caminho
engoliria o resto da frase.

Chamado no topo de `enrich_extracted_memories`, sobre `text` e `raw_content`,
antes de qualquer outra análise — um caminho destruído não pode ser gravado nem
casado com os segmentos técnicos da origem. Sem dano, o texto passa intocado.

### Testes

`openmemory/api/tests/test_escape_damage_repair.py`, 10 casos, usando como
fixture exatamente os bytes lidos da memória `a62a97a9`. Sem a correção o módulo
nem importa; com ela, 10/10 passam.

### Memórias já corrompidas

`python -m app.scripts.audit_escape_damage` — varreu 12.725 memórias, achou **11**
(0,09%). A correção é preventiva; estas continuam corrompidas até serem
reescritas.

| id | projeto | data | controles |
|---|---|---|---|
| `a62a97a9-66e6-4ddc-b7bc-e471c168d12d` | sysmo-api-tributacao | 28/08/2026 | 0x00, 0x09 |
| `b3ebf514-bd01-47f5-82d6-56eaf89e4ae7` | sysmo-s1 | 19/08/2026 | 0x09 |
| `25b23ab5-5ef8-45ad-8cbf-63695b6a4816` | sysmo-s1 | 19/08/2026 | 0x08 |
| `a5f471f6-7195-4673-a91e-e8ac6cfe05c1` | sysmo-s1 | 04/08/2026 | 0x09 |
| `8aa5e4fa-9e2f-4efc-b0cb-e588fb2f2206` | sysmo-s1 | 29/07/2026 | 0x09 |
| `edffd848-541d-43b2-937b-28969d6ca2f0` | sysmo-s1 | 29/07/2026 | 0x01 |
| `2b8524f1-468b-4be4-b076-c755f590e7b6` | sysmo-s1 | 29/07/2026 | 0x08 |
| `cbc259b3-4b7c-40ac-8cbe-200cbbd75883` | sysmo-s1 | 29/07/2026 | 0x0c |
| `edd0fda8-a407-46b8-8a5f-1407cb83b761` | sysmo-s1 | 29/07/2026 | 0x0c |
| `b9f5075a-60a8-4352-b4f6-7074fb86e43c` | sysmo-s1 | 03/07/2026 | 0x09 |
| `e47e50ac-4dd5-424f-81b7-4ca3d330372a` | default | 02/07/2026 | 0x09 |

As duas piores são a `a62a97a9` (caminho UNC de teste) e a `b9f5075a` (o
`{$R 'version.res' '..\..\..'}` dos `.dpr`, que virou uma sopa de `\text{..}`).
Não dá para reconstruí-las automaticamente: o texto de origem não está mais no
acervo. Recomendação: apagar as duas e regravar; as outras nove perderam só um
caractere de controle isolado no meio do texto e continuam legíveis.

---

## Problema 2 — ranking dominado por recência e `group`

### Fórmula atual

`app/utils/recency.py`, `rank_search_results`:

```
effective_score = score * recency^W * project * group
  recency = 0.5 ** (idade_dias / 90)     W = MEM0_SEARCH_RECENCY_WEIGHT = 1.0
  project = 1.1 se o nome bate (ou mesma família), 1.05 fuzzy, senão 1.0
  group   = 2.5 se autor e solicitante são do mesmo grupo, senão 1.0
```

Aplicada em `mcp_server.search_memory` (3 pontos) e `routers/compat_v3.search`.

### Medição

Quatro consultas reais, 100 candidatos cada, contra o acervo:

| consulta | score sem. | amplitude | recência | amplitude |
|---|---|---|---|---|
| `371145` | 0,511–0,577 | **0,066** | 0,606–1,000 | 0,394 |
| `371145 aliquotas efetivas endpoint entrega testes liberacao MR` | 0,668–0,819 | **0,151** | 0,606–1,000 | 0,395 |
| `MS-Tributacao pipeline de compilacao nativo GraalVM` | 0,661–0,800 | 0,138 | 0,605–1,000 | 0,395 |
| `DDA Sicoob importacao de arquivo retorno` | 0,662–0,778 | 0,116 | 0,605–0,975 | 0,370 |

Confirma o diagnóstico: a recência varre uma faixa **2,6x maior** que o sinal
semântico, e no caso do código puro, **6x**. E `sysmo-s1` é 74–98% de cada pool,
então o empurrão de 10% do `project` sobre meia dúzia de itens não separa nada.

### O que NÃO resolve

Reponderar. Medido — precisão@5, mesma verdade de base, mesmos pools:

| configuração | q1 | q2 | q3 | q4 |
|---|---|---|---|---|
| atual (`hl=90 W=1,0 proj=0,1 sem^1`) | 3/5 | 2/5 | 5/5 | 0/5 |
| semântico^3 | 2/5 | 1/5 | 5/5 | 2/5 |
| recência achatada (`W=0,35`) | 2/5 | 2/5 | 5/5 | 2/5 |
| projeto forte (`boost=0,6`) | 3/5 | 2/5 | 5/5 | 0/5 |
| `sem^3 + W=0,35 + proj 0,6` | 2/5 | 2/5 | 5/5 | 3/5 |
| `sem^4 + W=0,25 + proj 1,0` | 2/5 | 2/5 | 5/5 | 3/5 |
| puro semântico (`W=0`) | 1/5 | 0/5 | 5/5 | 2/5 |

Nenhuma combinação melhora a consulta que motivou tudo isso (q1/q2), e a melhor
delas é pior em q1. O motivo é direto: **amplificar um sinal que não existe não
cria discriminação**. Com amplitude semântica de 0,066, nenhum expoente separa a
memória certa da errada.

Por isso **não mexi** em `MEM0_SEARCH_RECENCY_*` nem em
`MEM0_SEARCH_PROJECT_BOOST_*`. Os números não sustentam a mudança.

### O que resolve

Casar a **chave exata**. `recency.py` ganhou `lexical_match_factor`: quando a
consulta traz um código de tarefa e ele aparece literalmente no texto ou no
projeto do resultado, o fator é `MEM0_SEARCH_LEXICAL_BOOST` (3,0); senão, 1,0.
Nunca penaliza.

Antes e depois, consulta q1, top-8 (efetivo / trecho):

```
ANTES                                                  DEPOIS (x3 lexical)
 1  2.100  A TAREFA #371145 (alíquotas efetivas...      1  6.300  A TAREFA #371145 (alíquotas efetivas...
 2  1.979  Em 01/09/2026, ordem de liberação...         2  5.936  Em 01/09/2026, ordem de liberação...
 3  1.831  A suíte de testes local vs ms_...            3  5.475  Em 27/08/2026, decisão arquitetural...
 4  1.825  Em 27/08/2026, decisão arquitetural...       4  5.428  Para testes, o diretório configurado...
 5  1.815  Tarefa #370267 (Crédito Presumido)...        5  4.594  Tarefa #371145 registrada em 19/08...
 6  1.809  Para testes, o diretório configurado...      6  4.341  O novo endpoint de alíquota efetiva...
 7  1.794  Além do endpoint de liberação... [s1]        7  1.831  A suíte de testes local vs ms_...
 8  1.768  TAREFA #376140, status Redmine... [376140]   8  1.815  Tarefa #370267 (Crédito Presumido)...
```

Precisão@5: **3/5 → 5/5**. As 6 memórias da 371145 presentes no pool ocupam
exatamente as 6 primeiras posições. Consulta sem código: fator 1,0 em tudo, ordem
idêntica à de hoje (coberto por teste).

### Over-fetch no caminho do hook

`compat_v3.search` calculava `fetch_k = top_k` quando não havia filtro de
metadados. Com `limit=5` do hook, isso significa **ranquear depois de truncar**:
o blend só reordenava 5 candidatos brutos e nunca poderia recuperar um sexto.

Medido (precisão@5):

| cenário | q1 | q2 | q3 | q4 |
|---|---|---|---|---|
| `fetch_k=5` (hoje) | 3/5 | 2/5 | 5/5 | 2/5 |
| `fetch_k=40`, fórmula atual | 3/5 | 2/5 | 5/5 | **1/5** |
| `fetch_k=40` + lexical | **5/5** | 2/5 | 5/5 | 1/5 |

Over-fetch sozinho é neutro ou levemente negativo — só dá à recência mais
candidatos recentes para promover. Por isso o over-fetch novo é **condicional à
consulta trazer um código de tarefa**, quando existe uma chave exata para
selecionar dentro do pool maior.

### Testes

`openmemory/api/tests/test_lexical_task_ranking.py`, 6 casos, incluindo o caso da
371145 (memória certa, mais antiga e com score menor, vencendo a recente
irrelevante) e a garantia de que consulta sem código não muda de ordem.

`tests/test_mcp_read_project.py` foi ajustado: o contrato de `ranking_factors`
agora expõe também `lexical`.

---

## Problema 3 — `project` fragmentado (PROPOSTA — nada aplicado)

### Levantamento

49 projetos, 12.725 memórias.

- **10 projetos são código de tarefa puro**, 358 memórias:
  `373963` (69), `374540` (69), `375255` (66), `375528` (59), `374954` (50),
  `369314` (24), `376140` (14), `370664` (7), `370631` (0), `371703` (0).
- **6 usam o prefixo `tarefa-NNNNNN`** e estão todos vazios.
- **33 são repositório/produto**, 12.275 memórias (`sysmo-s1` sozinho tem 10.913).

E o problema real: **176 tarefas identificáveis, 22 espalhadas por mais de um
`project`**. A 371145 está em `sysmo-api-tributacao` (24) e `sysmo-s1` (6). A pior
é a 370664, em cinco lugares: `sysmovs` (37), `sysmo-s1` (18), `370664` (7),
`sysmo-api-tributacao` (1), `sysmos1-modular` (1).

### Proposta

1. **Campo próprio `task`** no payload do Qdrant, string com o código de 6
   dígitos. Indexado como keyword: acrescentar `"task"` a `KEYWORD_INDEX_FIELDS`
   em `mem0/vector_stores/qdrant.py:208` (`project` continua sendo o
   `TENANT_FIELD`).
2. **Parâmetro de busca** `task: str | None` em `search_memory` e em
   `compat_v3.SearchRequest`. Quando presente, vira filtro duro
   (`{"task": <codigo>}`) — "tudo da tarefa 371145" passa a ser uma pergunta com
   resposta exata, independente de embedding.
3. **`add_memories` grava `task`** quando o chamador informa, e por inferência
   quando o texto traz `TAREFA #NNNNNN`.
4. **`project` volta a ser só repositório/produto.** Criar projeto com nome que é
   código de tarefa passa a ser rejeitado no write-path.

O boost lexical do problema 2 já cobre o caso do dia a dia sem migração nenhuma;
o campo `task` é o que torna a consulta **determinística**, e é pré-requisito
para o `/tarefa` parar de depender de busca semântica.

### Migração — script pronto, NÃO executado

`openmemory/api/app/scripts/migrate_task_field.py`. Dry-run rodado:

```
PLANO DE MIGRACAO DO CAMPO `task`
  so backfill de task ......... 1643 memorias
  backfill + troca de project . 0 memorias

  projetos que SAO codigo de tarefa e nao tem destino no --map:
    373963  69 | 374540  69 | 375255  66 | 375528  59
    374954  50 | 369314  24 | 376140  14 | 370664   7
```

Duas etapas, ambas incrementais e reversíveis:

1. **Backfill de `task`** em 1.643 memórias (as que já citam `TAREFA #NNNNNN` no
   texto ou cujo `project` é código). Não mexe em `project`. Sem risco.
2. **Reatribuição de `project`** só para as 358 do grupo "código de tarefa", e só
   com um mapa explícito `{"374954": "sysmo-s1", ...}` passado em `--map` — a
   decisão de para qual repositório cada tarefa vai é sua, não do script.

A escrita está **deliberadamente desabilitada**: `--apply` levanta `SystemExit`
com a mensagem de que depende da aprovação do plano e de um backup
(`python -m app.scripts.run_backup`). Preciso de duas coisas para seguir:

- **ok na proposta do campo `task`** (é mudança de payload e de contrato de busca);
- **o mapa código → repositório** para as 8 tarefas com memórias.

---

## Problema 4b — `supersedes` não é usado (PROPOSTA)

O caso: a memória de 06/08 diz que o endpoint "retorna cClassTrib > IBGE >
alíquotas ao Pricing"; a de 19/08 define que o MS **persiste em tabela e não
retorna a relação**. As duas estão `active`, e a antiga tem vantagem de recall.

A infra existe (`supersedes` em `add_memories`, `mark_obsolete`, `state`,
`utils/supersedes.py`, `utils/autodedup.py`) — o que falta é **obrigar a decisão**.

Proposta, em duas partes:

1. **Detecção no write-path.** Hoje `add_memories` é fire-and-forget por decisão
   de arquitetura (ADR-004: nenhum embed/LLM no request path), então a checagem
   não pode ficar lá. Fazer no **worker de escrita**, onde o embedding já é
   calculado: se a nova memória tem similaridade acima de um limiar
   (`MEM0_CONFLICT_SIMILARITY`, sugestão 0,90) com uma memória `active` do mesmo
   escopo e o chamador não declarou `supersedes`, a nova entra com
   `state = "pending_conflict"` e **não aparece na busca**. O MCP passa a expor
   `list_conflicts` / `resolve_conflict(id, supersedes=[...] | coexist=true)`.
   Assim o agente é forçado a decidir, mas o `add_memories` continua devolvendo
   `accepted` na hora.
2. **Comando de auditoria** `audit_contradictions`: para cada par de memórias
   `active` do mesmo escopo com similaridade acima do limiar, imprime as duas com
   data e id e sugere qual seria a revogada (a mais antiga). Roda sobre o acervo
   inteiro, não altera nada — o mesmo formato do `audit_escape_damage`. É o que
   limpa o passivo; o item 1 é o que impede novo passivo.

Sugiro começar pelo item 2: barato, sem risco, e o resultado dele é que diz se o
limiar de 0,90 está calibrado antes de ligar o bloqueio do item 1.

## Problema 4c — ruído no corpo das memórias (PROPOSTA)

O bloco `--- Original technical content ---` vem de
`mem0/memory/technical_content.py:format_memory_with_raw`, colado dentro do
**mesmo campo `text`** que a busca devolve e o hook injeta. É por isso que ele
custa contexto em toda injeção.

Não deve ser eliminado — foi feito de propósito para não perder script, SQL e
comando verbatim, e `test_technical_content_preservation.py` cobre isso. O erro é
estar no campo injetado.

Proposta: **mover, não truncar.**

- `raw_content` já existe como campo separado nas memórias extraídas; hoje ele é
  concatenado em `text` por `format_memory_with_raw` e o original se perde de
  vista.
- Gravar `raw_content` no payload como campo próprio e deixar em `text` só o
  resumo interpretado.
- `search_memory` e `compat_v3.search` devolvem `text`. Quem quiser o verbatim
  pede: `get_memory(id, include_raw=true)`.
- Truncar não resolve — um script cortado no meio é pior que nenhum script; e é
  exatamente o que se vê hoje nas memórias com o bloco cortado.

Vale medir a distribuição de tamanho antes de dimensionar o esforço — é o
primeiro passo se você quiser que eu siga com este.

---

## Arquivos alterados

| arquivo | o quê |
|---|---|
| `C:\Users\s258\.claude\mem0\mem0-hook.mjs` | reescrito (problema 1) |
| `C:\Users\s258\.claude\mem0\mem0-hook.test.mjs` | novo, 8 casos |
| `mem0/memory/technical_content.py` | `repair_escape_damage` + chamada em `enrich_extracted_memories` |
| `openmemory/api/app/utils/recency.py` | `extract_task_codes`, `lexical_match_factor`, `query=` em `rank_search_results` |
| `openmemory/api/app/mcp_server.py` | passa `query=` nas 3 chamadas de ranking |
| `openmemory/api/app/routers/compat_v3.py` | passa `query=`; over-fetch condicional |
| `openmemory/api/tests/test_escape_damage_repair.py` | novo, 10 casos |
| `openmemory/api/tests/test_lexical_task_ranking.py` | novo, 6 casos |
| `openmemory/api/tests/test_mcp_read_project.py` | contrato de `ranking_factors` inclui `lexical` |
| `openmemory/api/app/scripts/audit_escape_damage.py` | novo, relatório |
| `openmemory/api/app/scripts/migrate_task_field.py` | novo, dry-run, escrita desabilitada |

## Suíte

`openmemory/api/tests/`: **1381 passaram, 0 falharam** (130 s).
`C:\Users\s258\.claude\mem0`: `node --test`, **8 passaram**.
