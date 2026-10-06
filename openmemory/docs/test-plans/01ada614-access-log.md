# Plano de testes manuais de aceitação — Card 01ada614

**Card:** "Logs de acesso exibidos na UI não estão corretos"
**Branch:** `feat/melhorias-mem0-access-log` (base `feat/melhorias-mem0`)
**Escopo:** endpoint `GET /api/v1/memories/{id}/access-log`, auditoria de leituras (UI `api`/`admin`, MCP `mcp`, compat `compat_v3`) e componentes `AccessLog` (detalhe da memória) / detalhe admin.

> Fora de escopo: o rótulo de host MCP não verificado (B1 de exibição) é tratado no card 4307b5d8.

## Pré-requisitos

- Stack em homologação com a branch implantada (somente `openmemory-mcp`, `openmemory-write-worker` e UI rebuildados; **não** recriar `mem0_store`).
- Duas pessoas com login Google: **A** (com máquina vinculada `HOST-A` em status `linked`) e **B** (sem máquina vinculada).
- Um agente MCP (Cursor/Claude Code) rodando em `HOST-A`, conectado em `/mcp/<cliente>/sse/HOST-A`.
- Uma memória de teste `M` com ID conhecido, num projeto de teste (ex.: `qa-access-log`).
- Acesso ao Postgres em **somente leitura** para conferir `read_audit_logs` (sem `DELETE`/`TRUNCATE`).
- Antes de começar, anote: `SELECT count(*) FROM read_audit_logs WHERE memory_id = '<M>';` → **N0**.

## Caminho feliz

| # | Passo | Resultado esperado |
|---|-------|--------------------|
| H1 | A faz login Google na UI e abre `/memory/<M>`. | A aba "Log de Acesso" mostra uma entrada com **nome e foto de A**, chip de canal **Interface Web**, tipo **Leitura**. |
| H2 | A recarrega a página 5× em menos de 5 min. | A entrada de A fica **agrupada** (ex.: `6×`) com intervalo `hh:mm–hh:mm`; não aparecem 6 linhas separadas. |
| H3 | No agente MCP em `HOST-A`, rodar `search_memory("<termo que traga M>", project="qa-access-log")`. | Nova entrada com **nome e foto de A** (via máquina vinculada), canal **MCP · <cliente>**, tipo **Busca** e a **query** exibida (truncada se longa, completa no tooltip). |
| H4 | No agente MCP, rodar `list_memories("qa-access-log")`. | Entrada separada, canal MCP, tipo **Listagem**, sem query. |
| H5 | Chamar `POST /v3/memories/search/` (compat) com `x-openmemory-host: HOST-A`. | Entrada com canal **API (compat v3)**, pessoa A, tipo Busca, query visível. |
| H6 | Filtro "Agentes" no log. | Some a Interface Web; ficam só MCP e API. Paginação volta à página 1. Filtro "Web" mostra só leituras da UI. |
| H7 | Repetir H3 três vezes em 1 min. | Uma única entrada MCP `3×` (mesmo ator, mesmo tipo, dentro da janela de 5 min). |
| H8 | Esperar > 5 min e repetir H3. | Nova entrada separada (janela ancorada na leitura mais recente). |
| H9 | Admin abre `/admin/projects/qa-access-log/<M>` logado. | Mesmos campos (pessoa, canal, tipo, contagem) e filtro por canal funcionando; listagem admin gera leitura com canal "Interface Web (admin)". |
| H10 | Conferir no banco: `SELECT count(*) ... memory_id='<M>'`. | Valor = **N0 + todas as leituras feitas** (cada reload/busca é uma linha). Nenhuma linha removida; `raw_total` da resposta bate com o banco. |

## Caminhos tristes / segurança

| # | Passo | Resultado esperado |
|---|-------|--------------------|
| T1 | Abrir `/memory/<M>` **sem login** (aba anônima). | Entrada **"Interface Web (sem login)"**, sem foto, sem nome de pessoa. Banco grava `hostname = 'ui:anonymous'`. |
| T2 | Linhas históricas `ui:S0293` (ID fixo do build). | Exibidas como **"Interface Web (sem login)"**, sem foto — nunca como a dona da máquina S0293. |
| T3 | Sem login, chamar `GET /api/v1/memories/<M>?user_id=<UUID de B>`. | Leitura gravada como `ui:anonymous`; log **não** mostra B. |
| T4 | Agente MCP conectado em `/mcp/cursor/sse/ui:<UUID de B>`, roda `search_memory`. | Entrada canal MCP com o texto bruto como host, **sem nome/foto de B**. |
| T5 | Igual a T4 com `user_id` = e-mail de B, `User.user_id` de B ou UUID puro de B. | Nenhum vira pessoa; exibe o valor como host não resolvido. |
| T6 | Compat `POST /v3/memories/search/` com `x-openmemory-host: ui:<UUID de B>` ou `body.user_id = <e-mail de B>`. | Canal API, **sem** pessoa B, sem foto. |
| T7 | Máquina `HOST-X` existente mas **não vinculada** (pending/revoked) faz busca MCP. | Exibe o hostname, sem nome/foto. |
| T8 | Sessão Google **expirada** abrindo a memória. | Leitura gravada como anônima (ou redirect ao login); nunca atribuída à última pessoa. |
| T9 | Página carregando: observar a rede ao abrir `/memory/<M>` logado. | O `GET /api/v1/memories/<M>` é disparado **uma única vez**, só depois de a sessão estar decidida (sem leitura anônima "fantasma" antes do Bearer). |
| T10 | `GET .../access-log?channel=xyz`. | `422` (valores aceitos: `web`, `mcp`, `api`, `agents`). |
| T11 | `GET .../access-log?page_size=101`. | `422`. |
| T12 | Memória sem nenhuma leitura. | Mensagem de vazio, sem erro. |
| T13 | Memória com > 3000 leituras (ou `ACCESS_LOG_GROUP_SCAN_LIMIT` baixo em homologação). | Aviso de histórico parcial na UI; `grouping_truncated=true`. |
| T14 | `ACCESS_LOG_GROUP_WINDOW_SECONDS=0` no container de homologação. | Agrupamento desligado; uma linha por leitura. |
| T15 | Banco indisponível no momento da busca MCP (simulado em homologação). | A resposta do MCP chega normalmente; só é registrado warning de auditoria. |
| T16 | `?grouped=false`. | Uma entrada por linha; `total == raw_total`. |
| T17 | Conferir que nada foi apagado após todo o roteiro: `count(*)` em `read_audit_logs` e `write_audit_logs`. | Contagens só cresceram. |

## Critérios de aceite → evidência esperada

| Aceite | Casos |
|--------|-------|
| Nome e foto de quem leu via MCP/compat por máquina vinculada | H3, H5, T7 |
| Distinção Web × MCP × API | H1, H3, H5, H6 |
| Leituras agrupadas na janela | H2, H7, H8, T14, T16 |
| Tipo de acesso e query visíveis | H3, H4, H5 |
| Nenhuma linha de auditoria apagada | H10, T17 |
| Sessão Google obrigatória para a UI virar pessoa; `ui:S0293` anônimo | H1, T1, T2, T3, T8, T9 |
| Impossível forjar pessoa via `ui:<uuid>`, e-mail ou `user_id` no MCP/compat | T4, T5, T6 |
