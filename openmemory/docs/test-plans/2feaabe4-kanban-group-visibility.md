# Plano de testes manuais de aceitação — Card 2feaabe4

**Card:** "Mostrar os quadros kanban apenas do grupo do usuario"
**Branch:** `feat/melhorias-mem0-planka-groups` (base `feat/melhorias-mem0`)
**Escopo:** isolamento por grupo no PLANKA embed (home/sidebar, REST, anexos, websocket, notificações), reconciliação de acesso em `mem0-auth` (`ensureSharedAccess`), espelho ShareMem (`set_project_lifecycle`), `created_by_email` em workspaces Spec e o script `openmemory/scripts/audit-kanban-group-isolation.py`.
**Referência:** `openmemory/docs/runbooks/kanban-group-visibility.md`.

> Fora de escopo (NITs aceitos na Review 4): botão de arquivar visível para não-gerente (N1, recebe `NOT_ENOUGH_RIGHTS`); corrida residual de `CardSubscription` entre a limpeza e o `deleteOne` (N4); notificações criadas **antes** da revogação continuam em `GET /api/notifications` (N5).

## Pré-requisitos

- Stack em homologação com a branch implantada (ver **Pré-deploy**). **Não** recriar `mem0_store`.
- Antes de começar, anote `points_count` em `http://localhost:6333/collections/openmemory` → **P0**.
- Dois grupos com workspaces Spec espelhados: **A** (projeto/board `PA`/`BA`, card `CA` com anexo `FA`) e **B** (projeto/board `PB`/`BB`, card `CB` com anexo `FB`).
- Pessoas com login Google: **A1** (grupo A, gerente de `PA`), **A2** (grupo A, só membro), **B1** (grupo B). No modo ShareMem todos são ADMIN no PLANKA.
- Uma pessoa **L** em modo legado (`group = '*'`).
- Anote os IDs PLANKA de `PB`, `BB`, `CB` e `FB` (via B1 ou SQL somente leitura).
- Acesso ao PostgreSQL em **somente leitura** para conferências (sem `DELETE`/`TRUNCATE`).
- Navegador com DevTools (aba Network → WS) para observar eventos do Socket.IO.

## Caminho feliz

| # | Passo | Resultado esperado |
|---|-------|--------------------|
| H1 | A1 abre o embed do Kanban. | Home: seção **"Equipe"** com **todos** os quadros do grupo A (ativos/concluídos/arquivados). Não existem as seções "Outros" nem "Compartilhados comigo". Nenhum nome de `PB`/`BB` aparece. |
| H2 | A1 abre a sidebar. | Só projetos/boards do grupo A; `PB` ausente. |
| H3 | `GET /planka/api/projects` como A1 (DevTools). | Lista só projetos com ≥1 board visível ao grupo A; nenhum projeto vazio com nome exposto. |
| H4 | A2 (membro, não gerente) abre `BA`, cria card, edita lista e comenta em `CA`. | Tudo funciona (`board_membership` `editor`). |
| H5 | A1 (gerente de `PA`, projeto só com boards de A) cria board novo pela UI e renomeia o board. | Funciona: a gerência é mantida porque todos os boards do projeto são do grupo A. |
| H6 | A1 arquiva e depois conclui `PA` pela UI. | Funciona (só gerente pode `isArchived`/`isCompleted`). |
| H7 | A2 tenta arquivar `PA`. | Negado (`NOT_ENOUGH_RIGHTS`); `PA` inalterado. (N1: o botão aparece — aceito.) |
| H8 | L (modo legado `'*'`) abre o embed. | Vê **todos** os projetos/boards de A e B, abre `BB`, baixa `FB`. Nenhuma gerência de L é removida. |
| H9 | Espelho: criar workspace Spec novo no grupo A (UI ShareMem), criar card, mover entre colunas, arquivar o workspace. | PLANKA ganha **projeto próprio** do workspace com o board; o card é criado e movido; ao arquivar, o projeto fica arquivado. Nenhum warning `returned 404` no log da API. |
| H10 | Conferir o workspace criado em H9: `SELECT created_by, created_by_email FROM spec_workspaces WHERE id = '<id>';` | `created_by` inalterado (id da máquina/usuário) e `created_by_email` = e-mail da pessoa. Via MCP `legacy` → `NULL`. |
| H11 | A1 loga duas vezes seguidas (após o TTL de 30 s). | A segunda reconciliação não altera nada (idempotente): contagens de `board_membership`/`project_manager` de A1 iguais. |
| H12 | Rodar a auditoria (ver Pré-deploy) em ambiente saudável. | Listas vazias ou só informativas (`created_by_email` NULL em workspaces antigos). |

## Caminhos tristes / segurança

| # | Passo | Resultado esperado |
|---|-------|--------------------|
| T1 | A1 (ADMIN do grupo A) chama `GET /planka/api/projects/<PB>`. | **404**. |
| T2 | A1 chama `GET /planka/api/boards/<BB>`, `GET /planka/api/cards/<CB>`, `GET /planka/api/cards/<CB>/comments`, `GET /planka/api/boards/<BB>/actions`. | **404** em todas (sem atalho de ADMIN com a ponte ativa). |
| T3 | A1 chama `PATCH /planka/api/projects/<PB>` com `{"isArchived": true}` e depois `{"isCompleted": true}` e `{"name": "x"}`. | **404** em todas; `PB` inalterado (conferir com B1). |
| T4 | A1 tenta `POST /planka/api/projects/<PB>/project-managers` adicionando a si mesmo. | **404**/negado; nenhuma linha nova em `project_manager`. |
| T5 | A1 abre no navegador (com cookie do embed) `/planka/attachments/<FB>/download/<nome>` e a URL de thumbnail. | **404**; o arquivo não é entregue. Controle: `FA` baixa normalmente para A1. |
| T6 | A1 com o embed aberto (DevTools → WS); B1 cria projeto, renomeia `PB`, cria board em `PB`, cria e comenta card em `BB`. | A1 **não** recebe `projectCreate`, `projectUpdate`, `boardCreate`, `cardCreate`, `commentCreate` de B. A home de A1 não ganha itens de B. |
| T7 | Revogação com socket: dar a A2 membership manual em `BB` (cenário legado), A2 abre `BB` (entra em `board:<BB>`); então reconciliar (A2 recarrega o embed após o TTL). | A membership de A2 em `BB` some; o socket de A2 sai de `board:<BB>` na hora (B1 move card em `BB` → A2 não recebe evento); `BB` dá 404 para A2. |
| T8 | Revogação limpa inscrições: antes de T7, A2 comenta em `CB` (gera `CardSubscription`) e assina `BB`. Após a reconciliação, conferir `card_subscription`/`board_subscription`/`card_membership` de A2 nos cards de `BB` (SQL leitura). | Zero linhas de A2 em `BB`; tarefas de `BB` sem A2 como responsável. |
| T9 | Depois de T8, B1 comenta de novo em `CB`. | A2 **não** recebe `notificationCreate`; `GET /planka/api/notifications` de A2 não traz o comentário novo. (Notificações anteriores à revogação podem permanecer — N5.) |
| T10 | Revogação de gerência em projeto misto: A1 gerente de projeto com board de A e board de B (montar em homologação). A1 recarrega o embed. | A1 perde a gerência do projeto e as inscrições no board de B; mantém `editor` no board de A (cria/edita cards). Perde criar/renomear board (documentado). Log sem `destroyOne is not a function`. |
| T11 | Board sem mapeamento em `spec_planka_id_map` criado pelo admin interno. | Tratado como de outro grupo (fail-closed): invisível para A1/B1; aparece na auditoria. |
| T12 | Workspace com `group_id` NULL. | Board invisível para todos (exceto `'*'`); listado na auditoria. |
| T13 | Projeto antigo espelhado **sem** `project_manager` do DEFAULT_ADMIN (montar em homologação removendo a gerência via UI do PLANKA). Arquivar o workspace no ShareMem. | API não falha; log com warning `PLANKA project lifecycle PATCH returned 404 for project <id> ...`; projeto PLANKA não arquiva (divergente). |
| T14 | Rodar a auditoria após T13. | Projeto listado em "Projetos mapeados sem gerência do DEFAULT_ADMIN". |
| T15 | Aplicar o procedimento do runbook (SQL de reatribuição em `BEGIN`/`COMMIT` ou UI), rodar a auditoria de novo e `POST /admin/planka/resync`. | `INSERT 0 N` com N = itens da auditoria; auditoria vazia; projeto passa a arquivado conforme o Spec. Reexecutar o SQL → `INSERT 0 0`. |
| T16 | Auditoria é read-only: contar linhas de `planka.project_manager`, `planka.board_membership`, `spec_workspaces`, `spec_planka_id_map` antes e depois de rodar o script. Opcional: rodar com usuário sem permissão de escrita. | Contagens idênticas; o script abre `SET TRANSACTION READ ONLY` e termina com rollback; sem erro de permissão com usuário read-only. |
| T17 | Workspace criado via MCP `legacy` ou com hostname forjado no path. | `created_by_email` = `NULL` (nunca e-mail derivado do path). |
| T18 | Conferir ao fim do roteiro `points_count` do Qdrant. | Igual a **P0** (nenhum passo toca o Qdrant). |

## Pré-deploy

1. **Auditoria (somente leitura):**

   ```bash
   cd openmemory
   docker compose -f docker-compose.scale.yml exec openmemory-mcp \
     python /usr/src/openmemory/scripts/audit-kanban-group-isolation.py
   ```

   Anote "workspaces com `group_id` NULL", "boards sem mapeamento" e "Projetos mapeados sem gerência do DEFAULT_ADMIN".
2. **Reatribuição (escrita, só com decisão humana)** se a última lista não estiver vazia: SQL do runbook (seção *Pré-condição: DEFAULT_ADMIN gerente dos projetos espelhados*) em `BEGIN; ... COMMIT;`, conferindo `INSERT 0 N` contra a auditoria. Só adiciona gerência; não remove nada.
3. Anote `points_count` em `http://localhost:6333/collections/openmemory`.
4. **Rebuild somente** de `planka`, `openmemory-mcp` e `openmemory-write-worker` (decisão do tech lead):

   ```bash
   docker compose -f docker-compose.scale.yml build planka openmemory-mcp openmemory-write-worker
   docker compose -f docker-compose.scale.yml up -d --no-deps planka openmemory-mcp openmemory-write-worker
   ```

   **Nunca** incluir `mem0_store`; nunca `down -v`. `openmemory-mcp` aplica a migração `w1s2c3r4e5m6` (coluna anulável `created_by_email`).
5. Pós-deploy: `points_count` igual ao passo 3; rodar a auditoria de novo; `POST /admin/planka/resync` se houve reatribuição.

## Critérios de aceite → evidência esperada

| Aceite | Casos |
|--------|-------|
| Usuário do grupo X não vê nome de projetos/quadros do grupo Y (sidebar, home, REST) | H1, H2, H3, T1, T2 |
| ADMIN de outro grupo não abre, arquiva nem conclui projeto alheio | T1, T3, T4 |
| Anexos de outro grupo negados | T5 |
| Socket isolado e revogação remove das salas | T6, T7 |
| Revogação limpa inscrições e não notifica | T8, T9, T10 |
| Modo legado `'*'` continua vendo tudo | H8 |
| Espelho cria, move e arquiva em projeto próprio | H9 |
| Projeto antigo sem gerência do DEFAULT_ADMIN detectado e corrigível | T13, T14, T15 |
| Gerente e membro dentro do próprio grupo | H4, H5, H6, H7 |
| Auditoria é read-only | T16 |
| `created_by_email` só de identidade confiável | H10, T17 |
| Deploy não toca o Qdrant | Pré-deploy 3–5, T18 |
