# Plano de testes manuais de aceitação — Card 8e45acf8

**Card:** "Merge automatico de projetos move memorias e deixa projetos com 0 memorias criadas"
**Branch:** `feat/melhorias-mem0-merge-fix` (base `feat/melhorias-mem0`)
**Escopo:** job `merge_projects`, que passa a só gerar propostas; tabela `project_merge_proposals` (migration `m1p2r3o4p5s6`); approve/reject/apply via worker; `POST /api/v1/apps/{id}/rename`; `merge-preview`; relatório `merge-inconsistencies`; seção "Propostas de unificação" em `/admin/governance`; proxy da UI em modo legado. Referência: `openmemory/docs/runbooks/project-merge.md`.

> Fora de escopo: o backfill (desfazer os merges antigos). Ele é uma operação à parte, que só roda depois de `POST /admin/backup/run`, da conferência de `points_count` e de aprovação explícita do usuário.

## Pré-requisitos

- Homologação com a branch implantada. Rebuild **somente** de `openmemory-mcp`, `openmemory-write-worker`, `openmemory-governance-worker` e da UI. **Não** recriar `mem0_store`, **nunca** `down -v`, **não** habilitar `MEM0_ALLOW_*_DELETE`.
- Antes de começar, anote `points_count` de `http://localhost:6333/collections/openmemory` → **P0**.
- Rode `POST /admin/backup/run` antes do roteiro.
- `ADMIN_TOKEN` de homologação (`H="X-Admin-Token: $TOKEN"`) e um usuário admin logado na UI.
- Projetos de teste com memórias criadas via MCP, com contagens conhecidas:
  - `qa-merge-canon`: **C0** memórias;
  - `qa-merge-alias`: **A0** memórias, mais um spec workspace e um override de policy;
  - `qa-merge-alias2`: **A2** memórias;
  - `qa-merge-livre`: não existe (nem no catalog, nem no Qdrant).
- Postgres em **somente leitura** para conferir `project_merge_proposals`, `projects`, `write_queue` e `write_audit_logs`, sem `DELETE`/`TRUNCATE`.

## Caminho feliz

| # | Passo | Resultado esperado |
|---|-------|--------------------|
| H1 | Migration: `alembic upgrade head` no container da API e depois `alembic current`. | `m1p2r3o4p5s6 (head)`; a tabela `project_merge_proposals` existe; as tabelas existentes não mudam. |
| H2 | `GET /admin/governance/projects/merge-preview` (admin) envolvendo `qa-merge-canon`/`qa-merge-alias`. | Retorna grupos sugeridos com `memory_counts`; **nada** muda no SQL nem no Qdrant (`points_count` = P0; contagens C0/A0 intactas). |
| H3 | Disparar o job: `POST /admin/governance/projects/merge` (ou aguardar o agendamento). | O job termina `done` com `error = null` e grava **propostas `pending`**. Nenhuma memória muda de projeto. |
| H4 | `GET /admin/governance/projects/merge-proposals?status=pending` e conferir na UI em "Propostas de unificação". | A proposta `qa-merge-alias → qa-merge-canon` aparece com confiança, motivo e contagens. A listagem **não** traz `undo_info`. |
| H5 | Aprovar pela UI (botão Aprovar) ou via `POST .../merge-proposals/<id>/approve`. | Status `approved` e depois `applied`, depois que o governance-worker roda. `apply_job_id` é preenchido. |
| H6 | Conferir as contagens após o apply. | `qa-merge-canon` = **C0 + A0** memórias, a linha `qa-merge-alias` some de `projects`, `points_count` = **P0** (nada apagado) e `/api/v1/apps` mostra o canonical com a soma. |
| H7 | **Contagem de memórias criadas preservada:** conferir o card/`/api/v1/apps` de `qa-merge-canon` (memórias criadas) e o `write_queue`/audit repontado. | O total de criadas do canonical = soma de canonical + alias. Nenhum projeto fica com 0 criadas e com referências pendentes. O spec workspace e o override de policy do alias foram repontados ou mesclados sem erro de FK. |
| H8 | `GET .../merge-proposals/<id>` (detalhe). | `undo_info` traz o snapshot do alias, `qdrant_point_ids` (A0 ids), ids de `write_queue`/audit/spec repontados e `moved_memories = A0`. |
| H9 | Rejeitar: criar outra proposta (`qa-merge-alias2 → qa-merge-canon`) e clicar em Rejeitar. | Status `rejected`; nada é movido; `qa-merge-alias2` continua com A2. |
| H10 | Rename para nome livre: na UI, renomear `qa-merge-alias2` → `qa-merge-livre`. | `200`, aplicado na hora; a UI redireciona e mostra o total de memórias movidas; `qa-merge-livre` tem A2 memórias. |
| H11 | Rename para nome existente: na UI, renomear `qa-merge-livre` → `qa-merge-canon`. | `202 proposal_pending`; **toast informativo** "proposta pendente"; **sem** redirecionar; nada é movido até a aprovação. A proposta aparece em H4 com `origin = rename`. |
| H12 | Relatório: `GET /admin/governance/projects/merge-inconsistencies` após H5–H11. | `items` vazio para os projetos de teste, `orphan_scan = ok` e nenhum `orphan_qdrant_projects` referente a eles. |
| H13 | Migration revertida: em banco de homologação descartável (cópia), `alembic downgrade -1` e depois `upgrade head`. | O downgrade volta para `r0s1t2u3v4w5` e remove só `project_merge_proposals`; o re-upgrade volta para `m1p2r3o4p5s6`. Nenhuma outra tabela é afetada. **Não rodar em produção.** |

## Caminhos tristes / segurança

| # | Passo | Resultado esperado |
|---|-------|--------------------|
| T1 | **Governança pausada:** `processes_enabled.merge_projects = false` na policy. Tentar preview, enqueue (`/merge`, `/merge-now`), approve e o rename para nome existente. | Todos respondem `409`. Nenhuma proposta é aplicada. O agendamento não roda o job. O rename para nome livre continua `200`. |
| T2 | **Falha no Qdrant com compensação:** em homologação, interromper o acesso ao Qdrant (bloquear a rede só do worker, **sem** parar/recriar `mem0_store`) no meio da relocação de uma proposta aprovada. | O SQL sofre rollback, os pontos já movidos voltam ao projeto original e a proposta fica `failed` com `last_error`. Ao restaurar a conexão, "Tentar de novo" resolve e leva para `applied`. Se a reversão falhar, aparece o log `project merge compensation INCOMPLETE` com os ids e sobe `project_merge_compensation_failures_total`. |
| T3 | **Commit ambíguo:** derrubar a conexão do Postgres durante o `COMMIT` do apply (ex.: `pg_terminate_backend` do PID do worker no instante do commit). | O código relê numa sessão nova. Se a proposta está `applied` com o mesmo `apply_job_id`, **não** compensa (os pontos ficam no canonical). Se não está, compensa. Se a releitura falha, aparece o log `ambiguous commit and verification failed`, nada é compensado e o relatório de inconsistências mostra o estado. |
| T4 | **Alias sem elegível:** proposta cujo único alias tem partição dedicada (ou nenhum alias existe). Aprovar. | A proposta fica `failed` com `MergeRuleViolation`, o job **não** é reprocessado, o canonical **não** é criado/alterado e o Qdrant não é tocado. |
| T5 | **Nome protegido:** rename de/para `default`, `tarefa-123` ou `370631`, e approve de proposta envolvendo esses nomes. | `422` em todos. O LLM do job não propõe esses nomes. Nada é alterado. |
| T6 | **Sem admin → 401:** sem `X-Admin-Token`/sessão admin, chamar `merge-preview`, `merge`, `merge-now`, `merge-proposals` (GET/approve/reject), `merge-inconsistencies` e `POST /api/v1/apps/{id}/rename`. | `401` em todos; nenhuma alteração. |
| T7 | **AUTH_UI_REQUIRED=0 com o proxy:** UI em modo legado, sem login; abrir `/admin/governance` e renomear um projeto. | O proxy injeta `X-Admin-Token` nos GET de `merge-proposals`, `merge-inconsistencies`, `merge-preview` e no POST de rename. A lista de propostas carrega (sem 401). O rename para nome existente mostra o toast de 202. |
| T8 | **Qdrant indisponível no rename:** renomear para um nome sem linha no catalog com o Qdrant inacessível. | `503`. O nome **não** é tratado como livre e nada é aplicado. |
| T9 | **Rename para nome só no Qdrant:** destino com pontos mas sem linha em `projects`. | `202 proposal_pending` (não aplica direto). |
| T10 | **Approve concorrente:** duas abas aprovando a mesma proposta ao mesmo tempo, ou aprovar e rejeitar ao mesmo tempo. | Só uma ganha; a outra recebe `409`. Um único job é enfileirado. |
| T11 | **Proposta antiga com canonical sumido:** aprovar uma proposta cujo canonical foi renomeado/removido. | `failed`, sem recriar o canonical. |
| T12 | **Merges concorrentes com projetos em comum (Postgres):** aprovar duas propostas que compartilham projeto. | Elas serializam via advisory lock + `FOR UPDATE` (proposta e projetos), com `lock_timeout = 15s`. Se uma espera mais que isso, fica `failed` e o worker faz retry; ninguém fica preso indefinidamente. |
| T13 | **Rejeitar `approved` antes do worker:** aprovar e rejeitar logo em seguida. | O job vira no-op e termina `done`; nada é movido. |
| T14 | **Alias reaparece:** depois do merge, uma escrita MCP classificada no nome antigo. | O alias volta à lista; `merge-inconsistencies` aponta (`items` ou `orphan_qdrant_projects`); resolve-se com uma nova proposta. |
| T15 | Conferir ao fim do roteiro: `points_count`, `count(*)` de `write_queue` e de `write_audit_logs`. | `points_count` ≥ P0 (só cresce com escritas novas); as filas/audit só cresceram; **nenhuma memória apagada**. |

## Critérios de aceite → evidência esperada

| Aceite | Casos |
|--------|-------|
| SQL + flush antes do Qdrant; compensação se o commit falhar | T2, T3 |
| `SpecWorkspace.project_id` e FKs tratados (sem `spec_workspaces_project_id_fkey`) | H7, H8 |
| Contagem de memórias criadas preservada (nenhum projeto com 0 criadas indevidamente) | H6, H7, H12 |
| Reconciliação de alias com 0 pontos e referências SQL | H12, T14 |
| Nunca fundir em `default`, `tarefa-*` ou numéricos | T5 |
| Demais merges só com aprovação manual (job só propõe) | H3, H4, H5, H9, H11 |
| Merge pausado respeitado | T1 |
| Rotas de merge/rename exigem admin; modo legado funciona via proxy | T6, T7 |
| Migration aditiva, reversível e com head único | H1, H13 |
| Nada apagado | H6, T15 |
