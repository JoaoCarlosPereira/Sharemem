# Runbook — Kanban visível só para o grupo do usuário

Card `2feaabe4` ("Mostrar os quadros kanban apenas do grupo do usuario").

> **Proibido** (AGENTS.md): `docker compose down -v`, `docker volume rm mem0_storage`,
> recriar/reiniciar `mem0_store` (Qdrant), habilitar `MEM0_ALLOW_*_DELETE`, apagar
> linhas de `write_queue`/`write_audit_logs`. Este deploy **não** toca Qdrant.

## O que muda

| Área | Antes | Depois |
|------|-------|--------|
| Home do PLANKA (modo ShareMem) | "Equipe" = só projetos onde a pessoa é gerente; "Compartilhados comigo"; "Outros" (por ser ADMIN) | "Equipe" = **todos** os quadros do grupo; sem "Compartilhados comigo"/"Outros"; seções ativos/concluídos/arquivados mantidas |
| Outros grupos | Visíveis para ADMIN em "Outros"; projetos vazios com nome exposto | Somem para todos (inclusive ADMIN); `GET /api/projects/:id` → 404 |
| Acesso | Gerência de projeto (`project_manager`) | `board_membership` `editor` dos boards do grupo; gerência mantida só se **todos** os boards do projeto forem do grupo |
| ADMIN (no ShareMem todos são ADMIN) | Visão total de projeto compartilhado (REST e websocket) | Sem atalho ADMIN com a ponte ativa: REST e eventos de socket seguem gerência/membership + grupo |
| Responsável do card (`sync-card-assignee`) | Ganhava gerência do projeto | Ganha só `board_membership` `editor` do board |
| Workspace Spec | `created_by` = id da máquina (ex. `S0293`) | `created_by` inalterado **+** `created_by_email` (só sessão JWT ou agent token com máquina vinculada ao dono; nunca `legacy`) |

Modo legado da LAN (`group = '*'`) preservado: concede todos os boards e não remove gerências.

### Regra de acesso (fail-closed)

Com a ponte ativa (`AUTH_JWT_SECRET` definido), o atalho "ADMIN vê todo projeto
compartilhado" do PLANKA **só** vale para requisições com `mem0Auth.group = '*'`
(modo legado). Qualquer outra requisição — JWT de grupo, cookie de
`/attachments/*`, bearer `internal`/`omtk_`/`legacy` (ator `DEFAULT_ADMIN`) —
precisa de gerência do projeto ou `board_membership`. O ator técnico do espelho
continua funcionando porque é gerente dos projetos que ele mesmo cria.

- `PATCH /api/projects/:id`: sem gerência, exige membership em board visível ao
  grupo (senão 404). `isArchived`/`isCompleted` são **só de gerente**.
- Sockets do embed (JWT) entram na sala `@user:<id>`, então a revogação de
  membership/gerência tira o socket de `board:<id>` na hora.

### Eventos `boardCreate` (limitação documentada)

Com a ponte ativa, `boardCreate` vai só para gerentes do projeto e para quem tem
membership no board novo (nunca para "todos os ADMIN"). **Não** há filtro por
grupo do board no broadcast: quando o espelho cria o board, o mapeamento
`spec_planka_id_map` (que dá o grupo) ainda não existe, então filtrar ali seria
caro e incorreto. Como cada workspace Spec tem projeto próprio e a gerência de
projeto com board de outro grupo é removida na reconciliação, o vazamento
residual é só o `boardCreate` para gerente de projeto misto antes do próximo
ciclo de reconciliação (TTL 30 s); o board não abre (`boards/show` → 404).

## Efeito no próximo login (sem backfill)

A reconciliação acontece em `mem0-auth` → `ensureSharedAccess`, **por pessoa, no
primeiro request do embed** (com TTL de 30 s). Cada passo é isolado; falha em um não
impede os demais:

1. cria `board_membership` `editor` em todo board do grupo (promove `viewer` → `editor`);
2. remove `board_membership` de boards de outros grupos e tira a pessoa da sala
   `board:<id>` do socket;
3. **gerência de projeto** (decisão do tech lead): mantida quando todos os boards
   do projeto são do grupo da pessoa (preserva "criar board" pela UI, que exige
   gerente — `boards/create.js`); removida só quando o projeto tem board de outro
   grupo. Board sem mapeamento criado pelo admin interno conta como de outro
   grupo (fail-closed). A gerência do admin interno (`DEFAULT_ADMIN_EMAIL`,
   `admin@mem0.local`) e de projetos privados **nunca** é removida.

A reconciliação é idempotente: a segunda execução não altera nada.

Quem não logar continua com os vínculos antigos no banco, mas a **leitura**
(`projects/index`, `projects/show`, `boards/show`) já filtra pelo grupo, então não
vê nada de outro grupo.

Quem perde a gerência (projeto misto) perde: criar board pela UI,
renomear/reordenar board e configurar o projeto (`boards/update` exige gerente para `name`/`position`/…). Criar e editar
cards e listas exige só `board_membership.role = editor` (`cards/create.js`,
`lists/create.js`, `lists/update.js`), então continua funcionando.

Workspaces antigos ficam com `created_by_email = NULL` (não há backfill).

## Deploy

**Decisão do tech lead:** este card rebuilda `planka` **e também** `openmemory-mcp`
e `openmemory-write-worker` (exceção aceita à regra "só PLANKA"): o aceite 4
(`created_by_email`) e o aceite 5 dependem da API nova (migração Alembic +
`spec_auth`/`routers/specs.py`), e o write-worker usa a mesma imagem da API —
deixá-lo na imagem antiga criaria divergência de schema/código entre os dois.
Nenhum outro serviço é recriado.

Rebuild **somente** destes três serviços:

```bash
cd openmemory
docker compose -f docker-compose.scale.yml build planka openmemory-mcp openmemory-write-worker
docker compose -f docker-compose.scale.yml up -d --no-deps planka openmemory-mcp openmemory-write-worker
```

- `openmemory-mcp` roda `alembic upgrade head` na subida: aplica
  `w1s2c3r4e5m6` (coluna anulável `spec_workspaces.created_by_email`, sem
  índice; aditiva, sem alterar dados). Em `feat/melhorias-mem0` ela convive com
  `s1t2u3v4w5x6` (dedup) e `m1p2r3o4p5s6` (merge de projetos), todas sobre
  `r0s1t2u3v4w5`; a revision de merge `t1u2v3w4x5y6` une as três num head único.
- **Nunca** inclua `mem0_store` no comando. Antes/depois, confira
  `points_count` em `http://localhost:6333/collections/openmemory`: ele não deve mudar.

## Auditoria (somente leitura, opcional)

```bash
docker compose -f docker-compose.scale.yml exec openmemory-mcp \
  python /usr/src/openmemory/scripts/audit-kanban-group-isolation.py
```

O script não tem modo de escrita (transação `READ ONLY` no PostgreSQL). Ele lista:

- workspaces com `group_id` NULL. São fail-closed: o board fica invisível para todos.
  A correção é manual e exige decisão humana;
- workspaces sem `created_by_email` (só para informação);
- boards PLANKA sem mapeamento em `spec_planka_id_map`, com o e-mail do criador.
  Esses boards são classificados pelo grupo do criador; se o criador for o ator
  técnico, o board fica invisível;
- projetos mapeados em `spec_planka_id_map` **sem `project_manager` do
  DEFAULT_ADMIN** (`PLANKA_DEFAULT_ADMIN_EMAIL`, padrão `admin@mem0.local`; o
  script lê `PLANKA_DEFAULT_ADMIN_EMAIL`/`DEFAULT_ADMIN_EMAIL` do ambiente). Ver
  a próxima seção.

### Pré-condição: DEFAULT_ADMIN gerente dos projetos espelhados

Sem o atalho de ADMIN, o `PATCH /api/projects/:id` do espelho
(`set_project_lifecycle`: arquivar/concluir) exige que o ator DEFAULT_ADMIN seja
**gerente** do projeto. Se não for, o PLANKA devolve 404 e a API só registra o
warning `PLANKA project lifecycle PATCH returned 404 for project <id> ...` (não
levanta erro). Assim, arquivado/concluído no PLANKA fica divergente do Spec.
Projetos criados pelo espelho já nascem com essa gerência, e a reconciliação
nunca a remove. Projetos antigos ou mexidos à mão podem não ter.

A ordem pode ser antes ou depois do deploy. O passo é idempotente.

1. Rode a auditoria acima e anote a lista "Projetos mapeados sem gerência do
   DEFAULT_ADMIN". Se estiver vazia, nada a fazer.
2. Reatribua por um dos caminhos abaixo (é **escrita**: só com decisão humana).
   - **Pela UI do PLANKA:** entre como DEFAULT_ADMIN (ou como gerente atual do
     projeto) → abra o projeto → Configurações → Gerentes → adicione
     `admin@mem0.local`.
   - **Por SQL** (PostgreSQL, schema `planka`; troque o e-mail se
     `PLANKA_DEFAULT_ADMIN_EMAIL` for outro). Rode em transação e confira a
     contagem antes do `COMMIT`:

     ```sql
     BEGIN;
     INSERT INTO planka.project_manager (project_id, user_id, created_at, updated_at)
     SELECT p.id, u.id, now(), now()
       FROM public.spec_planka_id_map AS m
       JOIN planka.project AS p ON p.id::text = m.planka_id
       JOIN planka.user_account AS u ON lower(u.email) = lower('admin@mem0.local')
      WHERE m.entity_type = 'project'
        AND NOT EXISTS (
              SELECT 1 FROM planka.project_manager AS pm
               WHERE pm.project_id = p.id AND pm.user_id = u.id);
     -- "INSERT 0 N": N deve bater com a lista da auditoria.
     COMMIT;  -- ou ROLLBACK;
     ```

     `id` usa o padrão `next_id()` da tabela. A operação só **adiciona**
     gerência: não remove nada nem toca o Qdrant.
3. Rode a auditoria de novo: a lista deve vir vazia.
4. Para corrigir a divergência que já existe, rode o resync idempotente
   `POST /admin/planka/resync`. Ele reafirma `isArchived`/`isCompleted` a partir
   do status Spec e nunca apaga dados do Spec.

## Verificação

1. Uma pessoa do grupo X abre o embed: "Equipe" mostra todos os quadros de X e
   nenhum nome de Y; as seções "Outros" e "Compartilhados comigo" não aparecem.
2. `GET /planka/api/projects/<id de projeto de Y>` → 404.
3. Uma pessoa que tinha gerência antiga loga e o log não mostra
   `destroyOne is not a function`. Falhas parciais aparecem como
   `mem0-auth: shared access partially reconciled`.
4. Novo workspace (UI, ou MCP com agent token de máquina vinculada): `created_by_email` =
   e-mail da pessoa. Via MCP `legacy` fica `NULL`.
5. Pessoa do grupo X com o embed aberto não recebe eventos (`projectCreate`,
   `projectUpdate`, `boardCreate`) de projetos de Y.

## Rollback

Imagens anteriores de `planka` / `openmemory-mcp` / `openmemory-write-worker`.
A coluna `created_by_email` pode ficar (anulável e ignorada pelo código antigo).
As gerências removidas **não** voltam sozinhas; reatribua pelo PLANKA se precisar.
