# mem0 — rodada 2: higiene do acervo e o critério de aceite do ranking

Continuação de `mem0-recall-ranking-2026-09-01.md`, mesma branch e mesmo PR. A
ordem de execução foi a pedida: item 2 (alcance do `supersedes`), item 3
(procedência), item 1 (critério de aceite do ranking) por último, porque depende
dos dois.

Antes deles entrou o **campo `task`** — o item 3a da rodada 1, aprovado como
campo opcional. A migração dos dados continua **não executada**.

## Campo `task` (rodada 1, item 3a — aprovado)

`openmemory/api/app/utils/scope_keys.py`. Chave de tarefa como campo próprio,
opcional, indexado como keyword no Qdrant.

- **Escrita**: `add_memories(text, project, task=None, supersedes=None)`. A chave
  informada vence; se não vier, é inferida do texto apenas na forma explícita
  `TAREFA #NNNNNN`. Um número de 6 dígitos solto no meio da frase **não** vira
  chave — falso positivo aqui contamina um filtro que só vale se for confiável.
- **Leitura**: `search_memory(..., task="371145")` vira filtro exato, e é
  **ortogonal ao projeto** — a mesma tarefa vive em mais de um repositório, então
  restringir a tarefa não pode implicar restringir o projeto. Entra também na
  chave do cache de busca, senão uma consulta com `task` seria servida da entrada
  sem ele.
- `project` volta a ser só repositório/produto.

O boost lexical da rodada 1 continua cobrindo quem não informa `task`. O campo é
o que torna a consulta **determinística** em vez de só bem ranqueada.

## Item 2 — `supersedes` por ID não alcança a duplicata

### Diagnóstico

O mecanismo funciona; o alcance é que é curto. O que já existia:

- `_apply_supersedes` (write worker) marca obsoletos **os IDs que o chamador
  citou** — nada além.
- `app/utils/autodedup.py` procura duplicatas, mas **da memória NOVA**. Numa
  correção a memória nova diz o *oposto* da antiga, então a duplicata não é
  vizinha dela — é vizinha da **superada**. Nenhum código olhava para lá.

Por isso `558075ce` (29/07) foi superada e `bf2613bf` (06/08), afirmando a mesma
coisa, sobreviveu ativa.

### Correção

`openmemory/api/app/utils/supersede_fanout.py`:

- `find_sibling_candidates(client, superseded_ids, ...)` — parte do **texto da
  memória superada**, busca vizinhos acima de `MEM0_SUPERSEDE_SIBLING_SIMILARITY`
  (0,88), descarta os já obsoletos, a própria superada e a memória que fez a
  correção. Limiar mais frouxo que o do autodedup (0,95) de propósito: ali se
  procura duplicata literal, aqui uma paráfrase do mesmo fato já interessa,
  porque ela sobrevive à correção e volta na busca como verdade.
- **Nada é marcado em silêncio.** A função devolve candidatos; quem decide é
  quem chamou.

Integração em dois pontos:

- `mark_obsolete` passa a devolver `sibling_candidates` e `ingest_siblings` na
  resposta, com uma nota dizendo que continuam ativas e que ninguém as marcou.
  Os vizinhos são lidos **antes** de marcar — depois o filtro de estado os
  esconderia e a busca partiria de um texto fora de circulação.
- O write worker registra em `logger.warning` cada irmã que ficou ativa depois de
  um `supersedes`, com id, score e trecho.

### Passivo existente

`openmemory/api/app/scripts/audit_duplicate_groups.py` — agrupa o acervo por
equivalência semântica e separa em dois relatórios: **estados divergentes**
(alguém já superou uma e as irmãs continuam ativas) e **todas ativas**
(candidatas a consolidação). Não altera nada.

Precisa rodar **dentro do container da API** — o Qdrant é `mem0_store`, interno
ao compose, e o cliente falha fechado na estação (confirmado: a mensagem é
`FAIL-CLOSED (MEM0_LOCAL_ONLY)`). Ou seja:

```
docker compose exec api python -m app.scripts.audit_duplicate_groups
```

É o insumo para calibrar o limiar antes de ligar qualquer automatismo. Não rodei
porque não tenho acesso ao container.

### Sobre dedup no ingest

Já existe (`MEM0_AUTODEDUP_MODE` = off | report | apply, limiar 0,95) e está
**off**. Não liguei. A recomendação é rodar em `report` por um tempo e ler os
logs — que é exatamente o que o próprio módulo já diz. Ligar `apply` sem esse
período é mudar dado gravado no escuro.

### Testes

`tests/test_supersede_fanout.py`, 10 casos, com os ids reais do incidente.

## Item 3 — a extração fan-out multiplica o que precisa ser coerente

### Diagnóstico

Uma gravação de 01/09/2026 virou três fatos atômicos (`67737eaf`, `4702f46c`,
`32015e8e`), cada um com id, embedding e ciclo de vida próprios. Nada no payload
os ligava, então superar a decisão alcançava um e deixava dois.

### Correção

Campo `ingest_id` no payload, indexado como keyword. O valor é o **id do job de
escrita**, que já era único por submissão — não foi preciso inventar
identificador novo, só parar de jogá-lo fora.

`ingest_siblings(client, memory_ids)` devolve os outros fatos ativos nascidos da
mesma gravação, e `mark_obsolete` já os expõe na resposta. Memória anterior ao
campo não tem `ingest_id` e simplesmente não produz irmãs — ausência não é erro.

Convive com `task` em vez de competir: `task` agrupa por **assunto ao longo do
tempo**, `ingest_id` agrupa por **ato de gravação**. Nenhum dos dois substitui
`project`.

### Testes

`tests/test_scope_keys.py` (17 casos) e a classe `TestProcedencia` em
`tests/test_supersede_fanout.py`.

## Item 1 — o critério de aceite do ranking

### O efeito colateral é real, e o teste pegou

Montei o conjunto de regressão pedido: pares conhecidos (fato revogado × fato
correto), em configuração adversarial — o revogado é sempre o **mais antigo** e o
de **maior score semântico**, que é como ele ganha se a recência perder peso.

O primeiro resultado foi uma reprovação, e não de uma mudança de peso
hipotética: **da fórmula que já estava valendo**. O par:

| | idade | score semântico | recência (meia-vida 90d) | efetivo |
|---|---|---|---|---|
| revogado | 26 dias | 0,870 | 0,819 | **0,712** |
| correto | 4 dias | 0,700 | 0,970 | 0,679 |

O revogado vencia. 22 dias de diferença só valiam 1,18x contra os 1,24x da
vantagem semântica — a recência a 90 dias **não discriminava versão** dentro de
um mês, que é a janela em que quase tudo do acervo vive.

### A correção: recência mais afiada, não mais achatada

Isto vai na direção **contrária** à recomendação da rodada 1 (mais peso semântico
+ curva achatada), e é justamente o que a evidência desta rodada mandava. Medido
nas mesmas 4 consultas reais, precisão@5:

| meia-vida | q1 | q2 | q3 | q4 | par adversarial |
|---|---|---|---|---|---|
| 90 dias (era) | 5/5 | 2/5 | 5/5 | 1/5 | **revogado vence** |
| 60 dias | 5/5 | 2/5 | 5/5 | 1/5 | correto vence (margem 4%) |
| **45 dias** | **5/5** | **2/5** | **5/5** | **1/5** | **correto vence (margem 13%)** |
| 30 dias | 5/5 | 2/5 | 4/5 | 1/5 | correto vence |
| 21 dias | 5/5 | 2/5 | 4/5 | 1/5 | correto vence |

`MEM0_SEARCH_RECENCY_HALFLIFE_DAYS` passa de 90 para **45**: o ponto mais afiado
que **não custa nada** na precisão (o degrau está em 30, onde q3 cai) e ainda
deixa margem no critério de aceite.

A lógica por trás: com o fator lexical assumindo a discriminação de **assunto**,
a recência fica livre para fazer o que ela sabe fazer, que é distinguir **versão**
do mesmo fato. Os dois sinais pararam de disputar o mesmo trabalho.

Confirmado também no caso `558075ce` citado no prompt (34 dias, score 0,878, o
maior dos 20 resultados): a 45 dias ele cai para 0,520 efetivo contra 0,752 do
correto — folga muito maior que os 0,676 × 0,776 de antes.

### O critério ficou no código

`tests/test_ranking_regression_obsoletas.py` — 7 casos. Além do antes/depois de
precisão, qualquer mudança de peso agora tem de passar por:

1. **O fato correto sempre vence o revogado**, com e sem código de tarefa na
   consulta.
2. **O boost lexical não pode favorecer o revogado** — quando os dois citam a
   tarefa, o fator tem de ser igual nos dois lados do par, senão o boost por
   chave exata viraria um jeito novo de promover dado velho.
3. Uma fronteira documentada: o ranking **não lê `state`**. Quem esconde obsoleto
   é o filtro da busca. Se um dia um obsoleto chegar ao ranking, ele concorre — e
   o que regrediu foi o filtro, não a fórmula.

O comentário no `recency.py` carrega os números e o aviso explícito contra
achatar a curva, com o caso `558075ce` nominal.

## Arquivos da rodada 2

| arquivo | o quê |
|---|---|
| `openmemory/api/app/utils/scope_keys.py` | novo — `task` e `ingest_id` |
| `openmemory/api/app/utils/supersede_fanout.py` | novo — irmãs ativas e procedência |
| `openmemory/api/app/scripts/audit_duplicate_groups.py` | novo — passivo de duplicatas |
| `mem0/vector_stores/qdrant.py` | `task` e `ingest_id` em `KEYWORD_INDEX_FIELDS` |
| `openmemory/api/app/mcp_server.py` | `task` em `add_memories`/`search_memory`; `mark_obsolete` devolve irmãs |
| `openmemory/api/app/workers/write_worker.py` | grava `task`/`ingest_id`; registra irmãs ativas |
| `openmemory/api/app/utils/mcp_read_wrappers.py` | repassa `task` no wrapper de auditoria |
| `openmemory/api/app/utils/recency.py` | meia-vida 90 → 45, com a medição no comentário |
| `openmemory/api/tests/test_scope_keys.py` | novo, 17 casos |
| `openmemory/api/tests/test_supersede_fanout.py` | novo, 10 casos |
| `openmemory/api/tests/test_ranking_regression_obsoletas.py` | novo, 7 casos |

## Suíte, rodadas 1 e 2

`openmemory/api/tests/`: **1415 passaram, 0 falharam** (84 s).
`C:\Users\s258\.claude\mem0`: `node --test`, **8 passaram**.

## O que continua parado esperando decisão

1. **Migração dos dados** (`migrate_task_field.py`): 1.643 memórias receberiam
   `task`. Escrita desabilitada. O campo agora existe, então falta só o backfill.
2. **Auditoria de duplicatas**: precisa rodar dentro do container.
3. **`MEM0_AUTODEDUP_MODE=report`**: ligar e ler os logs antes de qualquer
   `apply`.
4. **Deploy**: o servidor em 192.168.3.213 continua rodando a versão antiga.

## Decidido: não mexer nas memórias já corrompidas

As 11 memórias com dano de escape ficam como estão, inclusive as 2
irrecuperáveis (`a62a97a9`, `b9f5075a`). O `repair_escape_damage` age no momento
da extração, então é **preventivo**: corrige o que for gravado daqui para frente
e não toca no que já está no acervo.

Consequência aceita: quem recuperar a `a62a97a9` continua recebendo o caminho UNC
quebrado. O impacto é baixo — são 11 registros em 12.725 (0,09%), o caminho em
questão é trivialmente re-derivável, e o acervo se corrige sozinho por uso: na
próxima vez que alguém gravar sobre o mesmo assunto a memória nova sai íntegra, e
o fan-out do `supersedes` (item 2) apresenta a antiga como candidata a
obsolescência.

O `audit_escape_damage` continua no repositório para reconferir depois do deploy:
se aparecer memória nova na lista, o reparo não está valendo.
