# Runbook — Merge de projetos (propostas + aprovação de admin)

## Regras

- O job `merge_projects`, agendado ou chamado por `POST /admin/governance/projects/merge`, **não aplica
  merges**. Ele só grava **propostas pendentes** na tabela `project_merge_proposals`.
  Nenhuma memória ou linha SQL muda até um admin aprovar.
- `default`, `tarefa-*` e nomes numéricos, como `370631`, **nunca** entram em merge nem em rename, seja como origem
  ou como destino. O LLM descarta esses nomes e o apply/rename os bloqueia com 422.
  **Decisão do tech lead:** o bloqueio vale também para a *origem* de um rename simples (ex.:
  renomear `default` ou `tarefa-123`) — esses buckets são referenciados por nome em hooks/Kanban
  e trocá-los silenciosamente quebraria essas integrações.
- `POST /api/v1/apps/{id}/rename` exige admin:
  - se o destino é novo, sem linha em `projects` e com 0 pontos no Qdrant, o rename é aplicado na hora;
  - se o destino já existe no catalog **ou** no Qdrant (`count > 0`), a rota **não aplica** nada,
    cria uma proposta e responde `202 proposal_pending`. Com o processo `merge_projects` pausado,
    a resposta é `409`;
  - se o Qdrant estiver indisponível, a resposta é `503`, porque não dá para saber se o destino existe.
- Pausa da policy (`processes_enabled.merge_projects = false`): preview, enqueue, approve e o
  rename-merge retornam `409`. Nada é reativado por padrão.

## Tabela `project_merge_proposals`

Migration aditiva `m1p2r3o4p5s6` (down_revision `r0s1t2u3v4w5`). O histórico é completo e paginado,
sem corte. Ciclo de status:

```
pending ──approve──▶ approved ──worker ok──▶ applied
   │                    │  └──worker falha──▶ failed ──approve (retry)──▶ approved
   └──reject──▶ rejected ◀──reject───────────────┘
```

As transições são atômicas (`UPDATE ... WHERE id=:id AND status IN (:esperados)` + rowcount).
Entre duas aprovações concorrentes, só uma vence e a outra recebe `409`.

`failed` acontece quando:
- o canonical não existe, nenhum alias existe ou nenhum alias é elegível (ex.: todos com
  partição dedicada); a transação sofre rollback e nada é alterado (nem o canonical é criado);
- há violação de regra determinística (`MergeRuleViolation`); o job **não** é reprocessado;
- o enqueue do job falhou na aprovação.

`undo_info` (só em `GET /merge-proposals/{id}`; a listagem traz apenas `moved_memories`) guarda,
para cada alias aplicado:

- o snapshot da linha original de `projects`;
- `memory_count` e os `qdrant_point_ids` movidos;
- os IDs de `write_queue`, `write_audit_logs`, `governance_jobs` e `spec_workspaces` repontados;
- as contagens de `read_audit_logs` e `token_usage_logs`;
- os overrides de policy e schedule.

Com isso dá para desfazer a operação manualmente: devolva `payload.project` aos pontos listados e
reponte as linhas SQL listadas.

## Ver, aprovar e rejeitar (todas as rotas exigem admin)

```bash
API=http://localhost:8765
TOKEN=...   # ADMIN_TOKEN (ou sessão JWT de admin)
H="X-Admin-Token: $TOKEN"
curl -s -H "$H" "$API/admin/governance/projects/merge-proposals?status=pending" | jq
curl -s -H "$H" "$API/admin/governance/projects/merge-proposals/<id>" | jq
curl -s -X POST -H "$H" -H 'content-type: application/json' \
  "$API/admin/governance/projects/merge-proposals/<id>/approve" -d '{"note":"ok, mesmo repo"}'
curl -s -X POST -H "$H" "$API/admin/governance/projects/merge-proposals/<id>/reject" -d '{}'
```

A UI (`/admin/governance`, seção "Propostas de unificação") lista as propostas e tem os botões
Aprovar, Rejeitar e "Tentar de novo" (este último para `failed`).

A aprovação enfileira um job `merge_projects` manual (`payload.proposal_id`, `job_id` fixado na
proposta), executado pelo `openmemory-governance-worker`. Rejeitar uma proposta `approved` antes da
execução transforma o job em no-op. Um job `done` fica com `error = null`, e o histórico de
tentativas está em `payload.error_history`, com as últimas 10 falhas.

## Consistência do apply

1. **Serialização:** no Postgres, o apply pega `pg_advisory_xact_lock(hashtext('project_merge:<nome>'))`
   e depois `SELECT ... FOR UPDATE` nas linhas de `projects` envolvidas, **em ordem alfabética**,
   o que evita deadlock. A proposta também é travada com `FOR UPDATE`. Os locks usam
   `SET LOCAL lock_timeout = '15s'`: se outra transação segurar os projetos por mais tempo, o
   apply falha (proposta `failed`, retry pelo worker) em vez de ficar preso. No SQLite, isso é no-op.
2. **SQL primeiro:** as referências são repontadas e um `flush` é feito antes de tocar o Qdrant, de modo que
   violações de FK/unique abortam com o Qdrant intacto.
3. **Qdrant:** a relocação usa `vs.update(id, payload={"project": canonical})`, uma atualização parcial que só mexe em
   `project`, igual à compensação.
4. Se a relocação ou o commit falhar, o SQL sofre rollback e os pontos já movidos voltam ao projeto original.
   Se a reversão falhar, aparece o log `project merge compensation INCOMPLETE` com os IDs e sobe a métrica
   `project_merge_compensation_failures_total`.

### Efeitos da transação aberta

Durante a relocação no Qdrant, que pode demorar em projetos grandes, a transação SQL fica **aberta**
com locks nas linhas de `projects` e nas linhas repontadas. Escritas concorrentes nesses projetos,
como o write-worker gravando `write_queue` e audit, **esperam** o commit. Outro merge com os mesmos
nomes fica bloqueado no advisory lock. Rode merges grandes fora do pico e monitore
`pg_stat_activity` (estado `idle in transaction` longo) se algo travar.

**O alias pode reaparecer.** Os locks não impedem que uma escrita MCP concorrente (ou logo depois
do commit) classificada para o nome antigo crie de novo a linha do alias em `projects` ou grave
pontos com `payload.project = <alias>`. O merge não fica inconsistente por isso, mas o alias volta
a aparecer na lista. O relatório de inconsistências cobre esse caso: pontos sem linha no catalog
aparecem em `orphan_qdrant_projects`, e uma linha recriada sem pontos mas com referências SQL
aparece em `items`. Resolva com uma nova proposta alias → canonical.

### Commit ambíguo

Se a conexão cair **durante** o `COMMIT` (`OperationalError` / `connection_invalidated`), o
cliente não sabe se o commit foi aplicado. O código **não compensa às cegas**: abre uma sessão nova
e confere o estado gravado:

- proposta aprovada: se ela está `applied` com o mesmo `apply_job_id`, o commit valeu e o Qdrant
  fica como está; senão, compensa;
- rename simples: se a linha do alias sumiu de `projects` (ou, sem linha de alias, o destino
  passou a existir), o commit valeu; senão, compensa.

Se nem a verificação conseguir ler o banco, **nada** é compensado (log `ambiguous commit and
verification failed`) e o erro sobe. Rode então o relatório de inconsistências (abaixo) e, se a
proposta estiver `applied` com pontos no alias, use os `qdrant_point_ids` de `undo_info` ou uma
nova proposta alias → canonical. Outros erros de commit (ex.: violação de constraint) continuam
revertendo o Qdrant normalmente.

## Relatório de inconsistências (somente leitura, exige admin)

```bash
curl -s -H "$H" "$API/admin/governance/projects/merge-inconsistencies" | jq
```

- `items`: projetos do catalog com **0 pontos no Qdrant** mas com referências SQL (write_queue
  `done`, audit, spec workspaces), que é a assinatura de um merge aplicado pela metade.
- `orphan_qdrant_projects`: valores de `payload.project` presentes no Qdrant que **não existem**
  no catalog. A busca usa `facet` do Qdrant sobre o índice de `project`, com custo de uma consulta.
  `orphan_scan` vale `ok`, `unavailable` ou `skipped`.
- `qdrant_errors`: projetos cuja contagem falhou. Erro do Qdrant **não** é tratado como 0 e por
  isso não gera falso positivo.

O relatório **não corrige nada**, porque o operador pode estar desfazendo um merge (backfill via
`write_queue`). Métrica: `project_merge_inconsistent_projects`.

## Deploy desta mudança

Aplique a migration e faça rebuild **somente** dos serviços de API e workers. Nunca mexa em `mem0_store`/Qdrant e nunca use `down -v`.
O merge dos heads Alembic paralelos é feito na integração (ver docstring da migration).

```bash
cd openmemory
docker compose -f docker-compose.scale.yml build openmemory-mcp
docker compose -f docker-compose.scale.yml up -d --no-deps \
  openmemory-mcp openmemory-write-worker openmemory-governance-worker
```

Antes e depois, confira `points_count` em `http://localhost:6333/collections/openmemory`.
Não habilite `MEM0_ALLOW_*_DELETE`, porque o merge não depende disso.
