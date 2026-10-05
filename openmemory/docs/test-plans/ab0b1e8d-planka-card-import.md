# Plano de testes manuais de aceitação — Card ab0b1e8d

**Card:** "Card criado na tela do Planka vira task" (escopo simplificado — card A)
**Branch:** `feat/melhorias-mem0-planka-sync` (base `feat/melhorias-mem0`)
**Escopo:** webhook PLANKA `cards/create.js` → `POST /api/v1/specs/planka/card-created`
(lista branca `authMethod === 'jwt'`), `import_planka_card` idempotente, commit imediato
do vínculo no espelho (`mirror_task`, `IntegrityError` → 409 → `502 mirror_failed`),
validação de IDs PLANKA (`[0-9]{1,32}`, fullmatch) e adoção no `claim_task`.
Runbook: `openmemory/docs/runbooks/planka-card-import.md`.

> Fora de escopo: backfill admin (card B, `2db6625e`) e endurecimento do token/IDs da
> bridge, `compare_digest`, `mirror_document`, listas/checklists, `X-Mem0-Mirror` em
> `cards/update.js` (card C, `0fbc3c9a`). Não existe fallback síncrono: um webhook
> perdido só é recuperado por reenvio manual (ou, no futuro, pelo backfill do card B).

## Pré-requisitos

- Stack em homologação com a branch implantada. Rebuild **somente** de `openmemory-mcp`,
  `openmemory-write-worker` e `planka` (`COMPOSE_PROFILES=sidecars ... up -d --build --no-deps`).
  **Não** recriar `mem0_store`, nunca `down -v`.
- Um workspace Spec de teste (ex.: `qa-planka-import`) já espelhado no PLANKA, com as
  colunas Tasks / Em andamento / Revisão de código / Fase de teste / Concluído / SDD
  mapeadas em `spec_planka_id_map` (`list:<status>`).
- Pessoa **A** com login na UI do PLANKA (sessão JWT). Um agente MCP (Cursor/Claude Code)
  conectado ao OpenMemory com identidade **A** e outro com identidade **B**.
- `PLANKA_INTERNAL_ACCESS_TOKEN` disponível para os `curl` dos casos de bridge
  (variável `$TOKEN`; `$OM=<URL do openmemory-mcp>`). Não colar o token em logs/tickets.
- Acesso ao Postgres em **somente leitura** (sem `DELETE`/`TRUNCATE`).
- Antes de começar, anote:
  - `SELECT count(*) FROM task_cards WHERE workspace_id = '<WS>';` → **T0**
  - `SELECT count(*) FROM spec_planka_id_map WHERE entity_type = 'task';` → **M0**
  - `SELECT count(*) FROM spec_audit_logs WHERE workspace_id = '<WS>';` → **A0**
- Deixe os logs abertos: `docker compose logs -f planka openmemory-mcp`.

## Caminho feliz

| # | Passo | Resultado esperado |
|---|-------|--------------------|
| H1 | A, logada na UI do PLANKA (JWT), cria o card "QA import 1" na coluna **Tasks** do board do workspace. | Em ≤ 3 s aparece no Spec (`list_tasks`/UI Spec) uma task "QA import 1" com status `tasks`, `assignee` vazio, `last_activity_at` nulo. `spec_planka_id_map` ganha 1 linha `entity_type='task'` com o `planka_id` do card. `spec_audit_logs` registra `import_planka_card` com `detail.source = card_created` e `actor` = `sub` da sessão de A. |
| H2 | A cria "QA import 2" direto na coluna **Revisão de código**. | Task criada com status `revisao_codigo`, sem dono e **sem** claim automático. Nenhuma linha em `task_status_history`. |
| H3 | A edita o título de "QA import 1" na UI e depois move para **Em andamento**. | Fluxo normal do bridge (`card-updated`/`card-moved`): a **mesma** task muda título e status. Continua existindo uma única task e um único vínculo para o card. |
| H4 | Agente MCP de A roda `claim_task` em "QA import 2" (sem dono, em Revisão de código). | **Adoção**: `claimed: true`, `assignee = A`, status **continua** `revisao_codigo`. `spec_audit_logs.action = adopt_task`; **nenhuma** linha nova em `task_status_history`. O card no PLANKA mostra A como responsável e permanece na coluna. |
| H5 | Agente MCP de A roda `claim_task` em "QA import 1" depois de ela voltar para **Tasks** (sem dono). | Claim normal: status vai para `em_andamento`, auditoria `claim_task`, 1 linha em `task_status_history` (`tasks → em_andamento`). |
| H6 | No Spec (agente MCP), criar uma task nova "QA espelho" no workspace. | O espelho cria o card no PLANKA e grava o vínculo **logo após** o POST (commit imediato). Como a criação vem com `authMethod = internal`, **não** há webhook `card-created`: no log do `openmemory-mcp` não aparece `planka_card_imported` para esse card, e a contagem de tasks sobe só 1. |
| H7 | Conferir contagens no fim do caminho feliz. | `task_cards` = T0 + 3 (QA import 1, QA import 2, QA espelho); vínculos de task = M0 + 3; nenhuma task duplicada para o mesmo `planka_id`. |

## Caminhos tristes / segurança

| # | Passo | Resultado esperado |
|---|-------|--------------------|
| T1 | **Reenvio**: repetir manualmente o webhook de "QA import 1": `curl -sS -X POST "$OM/api/v1/specs/planka/card-created" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"planka_card_id":"<id1>","planka_list_id":"<list_tasks>","name":"QA import 1"}'` (repetir 3×, inclusive em paralelo com `&`). | Todas as respostas: `{"applied": false, "reason": "already_mapped", "task_id": "<mesma task>"}`. Nenhuma task ou vínculo novo; a corrida é resolvida pela UniqueConstraint `(entity_type, planka_id)`, nunca com `500`. |
| T2 | **Espelho não reimporta**: reenviar manualmente o webhook com o `planka_card_id` do card "QA espelho" (H6). | `already_mapped` (o vínculo já foi commitado pelo espelho). Nenhuma task duplicada. |
| T3 | **Token omtk/legacy**: criar um card via API do PLANKA usando um token `omtk_…` (MCP/agente) e, separadamente, um token legacy (`Authorization: Bearer <token legado>`), em uma coluna mapeada. | O card é criado no PLANKA, mas **não** vira task: nenhum `card-created` é chamado (lista branca só `jwt`), nenhuma linha em `spec_planka_id_map`, nenhum `planka_card_imported` no log. Risco aceito: ficam como cards órfãos até o backfill (card B). |
| T4 | **Criação no PLANKA sem auth** (ou `AUTH_JWT_SECRET` ausente → `authMethod = disabled`/`public`). | Nenhum webhook disparado; nada importado. Sem erro para o usuário do PLANKA. |
| T5 | **Webhook sem Authorization**: `curl -X POST "$OM/api/v1/specs/planka/card-created" -H 'Content-Type: application/json' -d '{"planka_card_id":"9001","planka_list_id":"<list_tasks>"}'`. | `401`. Nada gravado. |
| T6 | **Webhook com token errado** (`-H "Authorization: Bearer errado"`). | `401`. Nada gravado. |
| T7 | **Kill switch**: definir `PLANKA_IMPORT_UI_CARDS=0` no `openmemory-mcp` (rebuild/restart só desse serviço), criar um card na UI com A (JWT). | O PLANKA chama o webhook, mas a resposta é `{"applied": false, "reason": "import_disabled"}`. Nenhuma task criada; o card fica só no PLANKA. Restaurar `PLANKA_IMPORT_UI_CARDS=1` (ou remover) e confirmar com um card novo que o import voltou. Reenviar manualmente o card criado durante o kill switch → task criada (`applied: true`). |
| T8 | **moved/updated sem vínculo**: com o kill switch ligado (T7), mover e editar o card criado durante o kill switch; ou via `curl` em `/api/v1/specs/planka/card-moved` e `/card-updated` com `planka_card_id` não mapeado. | Respostas com `reason: "not_mapped"`. Esses eventos **não** importam o card (não existe fallback). Nenhuma task nova. |
| T9 | **Coluna SDD / coluna criada à mão**: criar card com A na coluna **SDD** e numa coluna nova, criada à mão no board. | SDD → `reason: "document_list"`; coluna nova → `not_mapped`. Nada gravado. |
| T10 | **IDs inválidos** no webhook: `planka_card_id` = `"abc"`, `"../users/me"`, `"12\n"`, `"١٢٣"` (dígitos árabes), 33 dígitos; idem em `planka_list_id`. | `{"applied": false, "reason": "invalid_id"}` para todos. Nada gravado e **nenhuma** chamada HTTP do espelho ao PLANKA (sem `PATCH /api/users/me` nos logs do PLANKA). Com 32 dígitos ASCII o ID é aceito (vira `not_mapped` se a lista não existir). |
| T11 | **Log de alerta quando o webhook falha**: parar momentaneamente só o `openmemory-mcp` (ou apontar `OPENMEMORY_INTERNAL_URL` para um host inexistente no `planka`) e criar um card com A na UI. | O card é criado normalmente no PLANKA (sem erro na UI, tempo ≤ ~3 s). O log do `planka` mostra `warn: mem0 notify-spec-card-create failed: card <id> NOT imported as Spec task (recover with admin backfill)` com status/`timeout`. Nenhuma task criada. Após religar, reenviar o webhook manualmente (T1) → `applied: true`. |
| T12 | **Bridge sem configuração**: remover `OPENMEMORY_INTERNAL_URL` ou o token do `planka` (homologação) e criar um card. | Log `mem0 notify-spec-card-create: OPENMEMORY_INTERNAL_URL/token missing; skip`. Card mantido no PLANKA, nada importado. |
| T13 | **Task sem dono em Concluído**: A cria card direto em **Concluído** (vira task `concluido` sem dono). Agente de A roda `claim_task`. | `claimed: false`. Status e `assignee` inalterados; nenhuma auditoria `adopt_task`. |
| T14 | **Adoção x exclusividade**: depois de H4 (A dona de "QA import 2"), agente de B roda `claim_task` na mesma task. | `claimed: false`, `current_assignee = A`. Nada muda. |
| T15 | **Adoção concorrente**: A cria "QA import 3" em Fase de teste; agentes A e B rodam `claim_task` ao mesmo tempo. | Exatamente um vence (`claimed: true`, coluna mantida em `fase_teste`); o outro recebe `claimed: false` com o vencedor como `current_assignee`. Uma única auditoria `adopt_task`. |
| T16 | **Conflito do vínculo no espelho** (homologação, opcional): forçar um vínculo já existente para o `planka_id` que o PLANKA vai devolver (ex.: banco de homologação restaurado de snapshot). | Log `planka_mirror_link_conflict`, rollback e resposta REST `502 mirror_failed` (ou warning no caminho best-effort). **Nunca** `500`. |
| T17 | Conferir que nada foi apagado após todo o roteiro: contagens em `task_cards`, `spec_planka_id_map`, `spec_audit_logs`, `write_queue`, `write_audit_logs`. | Contagens só cresceram; nenhuma linha removida. Nenhum card do PLANKA foi apagado pelo import. |

## Critérios de aceite → evidência esperada

| Aceite | Casos |
|--------|-------|
| Card criado por pessoa na UI (JWT) vira task no workspace dono da lista, com status = coluna e sem dono | H1, H2, H7 |
| Lista branca `authMethod === 'jwt'`: `internal`, `omtk_`, `legacy` e sem auth nunca notificam | H6, T3, T4 |
| Endpoint autenticado com o token do bridge | T5, T6 |
| Import idempotente (reenvio e corrida) e espelho sem duplicata | T1, T2, H6 |
| Commit imediato do vínculo no espelho; conflito → 409/502, nunca 500 | H6, T2, T16 |
| Validação de IDs PLANKA (`[0-9]{1,32}`, fullmatch, só ASCII) | T10 |
| Kill switch `PLANKA_IMPORT_UI_CARDS` | T7 |
| Sem fallback: `card-moved`/`card-updated` sem vínculo → `not_mapped` | T8, H3 |
| Webhook perdido: card mantido + alerta no log do PLANKA; recuperação manual | T11, T12 |
| Adoção no claim (mantém coluna, `adopt_task`, sem history) e exclusividade | H4, H5, T14, T15 |
| Task sem dono em Concluído não é adotável | T13 |
| Nada destrutivo | T17 |
