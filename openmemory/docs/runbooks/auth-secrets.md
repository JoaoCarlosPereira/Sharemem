# Runbook — Autenticação por equipe e segredos

> Prontidão para produção, task_10/task_11 — ADR-006. Alvo LAN.

## Autenticação por equipe (`AUTH_MODE`)

A API valida um token por equipe na borda. Modos (env `AUTH_MODE`):

| Modo | Comportamento |
|------|---------------|
| `off` | Não valida (compatibilidade total com trust-on-LAN). |
| `warn` | **Default.** Valida e contabiliza/loga ausência/invalidez, mas **não bloqueia** GETs legados. Credencial Bearer **inválida** ⇒ 401. `Bearer local` (shim OAuth) ⇒ legado. |
| `enforce` | Rejeita `401` quando o token é ausente/inválido. |

O cliente envia o token em `X-API-Key: <token>` ou `Authorization: Bearer <token>`.
A equipe resolvida é registrada para auditoria (`team_var`); a atribuição por
hostname (ADR-003) permanece.

### Mutações destrutivas `/admin/*`

Mesmo em `AUTH_MODE=warn`, restore/purge/requeue-done/migration/promote exigem
admin autenticado:

| Credencial | Env |
|------------|-----|
| `X-Admin-Token: …` ou `Authorization: Bearer …` | `ADMIN_TOKEN` |
| Sessão JWT da UI (email opcionalmente allowlisted) | `AUTH_ADMIN_EMAILS` (vírgula; vazio = qualquer sessão válida) |

Recomendado em LAN de produção: `AUTH_MODE=enforce` + `ADMIN_TOKEN` forte.

### Configuração do LLM/embedder (`/api/v1/config/*`)

**Todas** as rotas de `/api/v1/config` (GET, PUT, PATCH, `POST /reset` e as
sub-rotas `mem0/llm`, `mem0/embedder`, `mem0/vector_store`, `openmemory`) exigem
a mesma credencial admin acima, em qualquer `AUTH_MODE` — inclusive leitura, pois
a config contém a `api_key` do LLM e o `openai_base_url` (escrita anônima
permitiria repontar o LLM para um servidor de terceiros). Sem credencial ⇒ `401`;
sessão fora de `AUTH_ADMIN_EMAILS` ⇒ `403`.

- As respostas **sempre** mascaram segredos (`api_key`, `password`, `token`,
  `*_secret`, URLs com senha) como `****` + 4 últimos caracteres. Valores
  `env:VAR` são exibidos como estão.
  O mascaramento percorre dicts **e listas** em qualquer nível, inclusive URLs
  com segredo na query string (`?api_key=`, `token=`, `password=`…) e headers
  sensíveis (`Authorization`, `X-Api-Key`, `Cookie`…). É aplicado por uma
  `route_class` do router, então rotas novas em `/api/v1/config` herdam.
- Ida-e-volta segura: reenviar no PUT/PATCH o valor mascarado devolvido pelo GET
  **mantém** o segredo gravado (dicts por chave, listas por índice).
- Garantia final: se, após a restauração, sobrar valor `****…` numa posição que
  seria mascarada (chave sensível ou URL com credencial — ex.: lista mudou de
  tamanho, segredo sem valor anterior), a escrita é rejeitada com `422` — a
  máscara nunca é persistida. Texto livre (ex.:
  `openmemory.custom_instructions`) começando com `****` **não** bloqueia a
  gravação. Sob chave sensível, valores reais que comecem com `****` são
  tratados como máscara (falso positivo aceito); use `env:VAR`.
- Erros `422` de **validação do FastAPI/Pydantic** (corpo malformado, tipo
  errado) podem ecoar no campo `input` o valor enviado — inclusive um segredo
  digitado. Só quem já passou pelo `require_admin` recebe essa resposta (o 401
  vem antes), mas não registre corpos de resposta 422 em logs compartilhados.
- Na tela Configurações, apagar a máscara do campo de chave (deixar vazio)
  **mantém** a chave atual; para removê-la use o botão "Remover chave".
- Scripts: `run.sh` repassa `ADMIN_TOKEN` aos containers da API e da UI (se
  ausente, gera com `openssl rand -hex 32` e grava em `.openmemory-admin-token`,
  modo 600) e avisa se o seed do vector store falhar. `scripts/rebuild-ui-api.sh`
  aguarda a API (laço de prontidão, `READY_TIMEOUT_S`, padrão 90s) e faz um smoke
  **somente leitura** (anônimo ⇒ 401/403; com token ⇒ 200 e `api_key`
  mascarada; HTTP 000 ⇒ "API não respondeu"; 2xx anônimo ⇒ "erro de
  segurança") e lê o token do ambiente ou do `.env`. Todo `curl` tem timeout. Ambos passam o
  header por arquivo temporário (`curl -H @arquivo`), fora da linha de comando.

```bash
# Evite o token na linha de comando (visível em `ps`/histórico):
printf 'X-Admin-Token: %s\n' "$ADMIN_TOKEN" > /tmp/om-hdr && chmod 600 /tmp/om-hdr
curl -H @/tmp/om-hdr http://localhost:8765/api/v1/config
```

### UI legado (sem Google)

| Env | Efeito |
|-----|--------|
| `AUTH_UI_REQUIRED=0` | Middleware não força `/login`; login oferece “Continuar sem login”. |
| (vazio) + sem `GOOGLE_CLIENT_ID` | Mesmo comportamento legado (auto-detect). |
| `AUTH_UI_REQUIRED=1` ou Google configurado | UI exige sessão Google. |

No modo legado, o container da UI recebe `ADMIN_TOKEN` (server-only) e o
route handler `/api-proxy` injeta `X-Admin-Token` nas mutações `/admin/*`
quando a requisição chega sem `Authorization` nem `X-Admin-Token`. Assim
Backup / Restore / write-queue admin funcionam sem login Google, sem expor o
segredo no bundle `NEXT_PUBLIC_*`. Com sessão Google, o Bearer da sessão
prevalece e o proxy não sobrescreve.

O mesmo vale para `/api/v1/config/*` em **todos** os métodos (a tela
Configurações faz GET). A injeção de `ADMIN_TOKEN` (config **e** mutações
`/admin/*`) acontece **somente em UI legado** (`AUTH_UI_REQUIRED=0` / sem
Google). Com login Google exigido, o proxy não injeta nada: as telas admin usam o
Bearer da sessão (o `/api-proxy` fica fora do middleware de login, então injetar
ali daria poderes admin a qualquer anônimo que alcance a UI).

O `/api-proxy` rejeita com `400` caminhos em que algum segmento, depois de
decodificado e dividido por `/` ou `\`, tenha pedaço vazio, `.` ou `..` (ex.:
`admin/..%2f..%2fapi%2fv1%2fconfig`), além de caracteres de controle e
`.`/`..` duplamente codificados. `/` decodificado dentro de um segmento é
repassado como separador (como antes do card: `store/skills/team%2Fnew-skill/latest`
chega à API como `store/skills/team/new-skill/latest`, resolvido por
`{name:path}`) e `%` literal sai como `%25`. A injeção é decidida pelo pathname
upstream final normalizado — não pelos segmentos crus.

> **Atenção — modo legado não protege a config contra quem alcança a UI.**
> Com `AUTH_UI_REQUIRED=0`, qualquer pessoa que alcance a porta **3000** consegue,
> via `/api-proxy`, **ler** a config (com segredos mascarados) e **gravá-la**
> (inclusive repontar `openai_base_url`) e disparar mutações `/admin/*`, pois o
> proxy injeta o `ADMIN_TOKEN` server-side. A proteção do card só fica completa
> na porta 8765. **Em produção use `AUTH_UI_REQUIRED=1`** (login Google) e,
> recomendado, `AUTH_ADMIN_EMAILS` restringindo quem é admin; ou restrinja o
> acesso de rede à porta 3000.

### CORS

`CORS_ORIGINS` — lista separada por vírgula (ex.: `http://localhost:3000,http://192.168.2.184:3000`).
Sem a variável, a API usa `localhost:3000` / `127.0.0.1:3000` e `NEXTAUTH_URL` se setado.
**Não** use `*` com credentials.

### Governance purge

`MEM0_ALLOW_GOVERNANCE_PURGE=0` (default). Purge/cold-tier no Qdrant só rodam com
esta flag ou com o par `MEM0_ALLOW_MEMORY_DELETE` + `MEM0_ALLOW_BULK_DELETE`.

### Rollout recomendado (sem quebrar clientes)

1. Distribua os tokens às equipes e configure os clientes MCP com o header.
2. Suba em `AUTH_MODE=warn` e acompanhe a métrica `auth_denied_total{mode="warn"}`.
3. Quando `auth_denied_total` zerar (todos os clientes migraram), vire `AUTH_MODE=enforce`.

## Fonte dos tokens (fora do `.env` versionado)

Prioridade de carga (`load_team_tokens`):
1. `AUTH_TOKENS_FILE` — caminho de um secret montado. JSON `{ "<equipe>": "<token>" }` ou linhas `equipe:token`.
2. `AUTH_TOKENS` — inline `equipe1:tok1,equipe2:tok2` (apenas dev).

### Docker secret (produção)

Crie o arquivo de tokens **fora** do repositório e monte como secret:

```yaml
# trecho do compose (não versionar o arquivo de tokens)
secrets:
  team_tokens:
    file: ./secrets/team_tokens.json   # fora do controle de versão

services:
  openmemory-mcp:
    secrets: [team_tokens]
    environment:
      AUTH_MODE: enforce
      AUTH_TOKENS_FILE: /run/secrets/team_tokens
```

O default de `AUTH_TOKENS_FILE` já aponta para `/run/secrets/team_tokens`.

## Limpeza do `.env` versionado

- Nenhum valor sensível (tokens, `S3_SECRET_KEY`, senha do PostgreSQL, `API_KEY`)
  deve permanecer em `.env` versionado. Use Docker secrets ou um `.env` local
  não rastreado (já em `.gitignore`).
- `S3_ACCESS_KEY`/`S3_SECRET_KEY` do MinIO: **obrigatórios** no `.env` (compose
  scale/backup não embutem mais o default `minioadmin`).

## Rate limit (contexto)

Os limites por `(project, hostname)` (task_10) são configuráveis por env:
`RL_SEARCH_PER_MIN` (30), `RL_WRITE_PER_MIN` (60), `RL_BURST` (10),
`RL_BURST_WINDOW` (10). Respostas `429` trazem `Retry-After`.
