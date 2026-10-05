# Runbook — Card criado na tela do PLANKA vira task Spec (PLANKA → Spec)

Um card criado por uma pessoa na UI do PLANKA passa a existir como `TaskCard`
do workspace Spec dono do board. Spec continua a fonte de verdade (ADR-005).

## Como funciona

1. A pessoa cria o card na UI (sessão **JWT**).
2. O server PLANKA (`cards/create.js` → helper `notify-spec-card-create`, timeout 3 s)
   chama `POST /api/v1/specs/planka/card-created` com o Bearer do bridge.
3. O OpenMemory cria a task e grava o vínculo em `spec_planka_id_map`.

### Quem dispara o webhook

Lista branca: só `authMethod === 'jwt'` (pessoa logada na UI). `internal` (espelho
Spec → PLANKA), `omtk_`, `legacy` e requisições sem auth **nunca** notificam. O
espelho já grava o vínculo (commit) logo após criar o card no PLANKA; por isso o
card dele nunca vira task duplicada.

### Regras de import

- **Workspace**: o dono da *lista* do card (`list:<status>` em `spec_planka_id_map`).
- **Status**: a coluna em que o card foi criado (Tasks / Em andamento / Revisão de
  código / Fase de teste / Concluído).
- **Sem claim**: `assignee` vazio e `last_activity_at` nulo (o worker de timeout não
  libera o que nunca foi reivindicado).
- **Idempotente**: reenvio do mesmo card → `{"applied": false, "reason": "already_mapped"}`.
  Corrida entre dois eventos do mesmo card é resolvida pela UniqueConstraint
  `(entity_type, planka_id)`.
- **Ignorados** (respondem `applied: false`): coluna **SDD** (`document_list`),
  colunas criadas à mão (`not_mapped`), cards de documento já mapeados
  (`document_card`) e IDs PLANKA inválidos (`invalid_id`: `planka_card_id` e
  `planka_list_id` precisam casar `[0-9]{1,32}` por inteiro, só com dígitos ASCII).
  Nada é gravado nesses casos.
- **Auth**: sem `Authorization` ou com token errado → `401`.
- **Kill switch**: `PLANKA_IMPORT_UI_CARDS=0` no `openmemory-mcp` desliga o webhook
  (`reason: "import_disabled"`).
- **Eventos seguintes**: `card-updated` e `card-moved` do card importado seguem o
  fluxo normal do bridge. Card **sem** vínculo responde `not_mapped`. Esses eventos
  **não** importam o card.
- **Conflito no espelho**: se o `POST` do espelho criar um card cujo ID já está
  vinculado a outra task, o commit do vínculo bate na UniqueConstraint. Resultado:
  rollback, log `planka_mirror_link_conflict` e `PlankaMirrorError(409)`. Isso vira
  o `502 mirror_failed` de sempre (REST) ou warning (best-effort), **nunca 500**.
- **Não destrutivo**: o import não apaga nada nem escreve no PLANKA. Cada import grava
  `spec_audit_logs.action = import_planka_card` (`detail.source = card_created`).

### Webhook perdido

A notificação é best-effort: se ela falhar (OpenMemory fora, timeout, erro HTTP), o
card **continua** no PLANKA e o server PLANKA registra um alerta no log:

```
warn: mem0 notify-spec-card-create failed: card <id> NOT imported as Spec task (recover with admin backfill)
```

Um webhook perdido **só** é recuperado pelo **backfill admin** (card B, `2db6625e`).
Até ele existir, reenvie o webhook manualmente (é idempotente):

```bash
curl -sS -X POST "$OPENMEMORY_URL/api/v1/specs/planka/card-created" \
  -H "Authorization: Bearer $PLANKA_INTERNAL_ACCESS_TOKEN" -H 'Content-Type: application/json' \
  -d '{"planka_card_id": "<card_id>", "planka_list_id": "<list_id>", "name": "<título>", "actor": "<quem pediu>"}'
```

## Assumir um card importado fora de Tasks (adoção no claim)

Use `claim_task` (MCP) ou `POST /api/v1/specs/tasks/{id}/claim`. Uma task **sem
dono** em Em andamento / Revisão de código / Fase de teste é *adotada*:

- o chamador vira `assignee`;
- a **coluna é mantida**: não há transição, então nenhuma regra de pipeline é pulada;
- o lease começa a contar.

A adoção grava a auditoria `spec_audit_logs.action = adopt_task` e não grava linha em
`task_status_history` (o status não muda). Uma task sem dono em **Concluído** não é
adotável (`claimed:false`). Uma task com outro dono continua barrada pela
exclusividade.

## Deploy

Rebuild **somente** dos serviços alterados (nunca `down -v`, nunca recriar `mem0_store`):

```bash
cd openmemory
# planka está no profile "sidecars": sem COMPOSE_PROFILES o serviço não é reconstruído.
COMPOSE_PROFILES=sidecars docker compose -f docker-compose.scale.yml up -d --build --no-deps \
  openmemory-mcp openmemory-write-worker planka
```

- `openmemory-mcp` / `openmemory-write-worker`: endpoint `card-created` e adoção no claim.
- `planka`: webhook `card-created`.
- Sem migration Alembic (usa a tabela `spec_planka_id_map` existente).

## Fora de escopo

- **Card B** (`2db6625e`): backfill admin (`import_board_cards`, `import_all_boards`,
  `POST /api/v1/specs/workspaces/{id}/planka/import` e `POST /admin/planka/import`).
- **Card C** (`0fbc3c9a`): `compare_digest` no token do bridge, validação de ID em
  `mirror_document`, listas/checklists e `cards/update.js`.
