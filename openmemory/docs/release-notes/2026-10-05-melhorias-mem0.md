# ShareMem: novidades da liberação "Melhorias do Mem0"

**Data:** 05/10/2026
**PR:** [#39: feat: melhorias do Mem0](https://github.com/JoaoCarlosPereira/mem0-shared/pull/39)

Esta liberação traz 10 melhorias. Cada uma passou por revisão de código independente e por QA, e os 1968 testes da API e os 484 testes da UI passam.

---

## O que muda para quem usa no dia a dia

### Kanban (Planka)

**Cada equipe vê apenas os próprios quadros**
- A barra lateral e a home mostram somente os projetos e quadros do seu grupo. A seção "Outros", que misturava quadros de outras equipes, deixa de aparecer.
- O isolamento vale também para anexos, notificações e atualizações em tempo real.
- Ao perder acesso a um quadro, a pessoa para de receber as notificações e atualizações em tempo real dele.

**Card criado na tela do Planka vira task no ShareMem**
- Antes, só os cards criados pelos agentes (MCP) apareciam nas tasks. Agora o card criado manualmente no Planka também aparece, na coluna correspondente e sem responsável.
- Ao chamar `claim_task` numa task sem dono que já está fora do backlog (Em andamento, Revisão ou Teste), o agente a assume e ela permanece na mesma coluna.

### Memórias e projetos

**Merge de projetos não "esvazia" mais projetos**
- Corrigido o bug em que projetos ficavam com 0 memórias criadas e várias memórias acessadas.
- O merge automático **não aplica nada sozinho**: ele gera **propostas**, que um admin aprova ou rejeita em *Admin → Governança*.
- Renomear um projeto para um nome que já existe gera uma proposta de merge e mostra um aviso, em vez de fundir os projetos na hora.
- Projetos como `default`, `tarefa-*` ou nomes numéricos nunca viram o projeto principal de um merge.

**Projeto com espaços no nome**
- Salvar memória em projeto com espaços no nome deixou de dar erro: os espaços viram hífen (por exemplo, `Meu Projeto` → `Meu-Projeto`). A resposta informa o nome efetivamente usado.

### Log de acesso das memórias

- O log mostra **quem realmente acessou**, com nome e foto, em vez de repetir sempre o mesmo usuário.
- Mostra também o tipo de acesso (get, search ou list), a origem (Web ou MCP) e a busca feita.
- Leituras repetidas aparecem agrupadas na tela, mas o histórico completo continua guardado.

---

## O que muda para admins e operação

| Melhoria | O que muda |
|---|---|
| **Segurança de `/api/v1/config`** | Qualquer acesso, inclusive leitura, exige credencial de admin. As respostas sempre mascaram os segredos (`****` + 4 últimos caracteres). |
| **Arquivar task** | Novas ferramentas MCP `archive_task` e `unarchive_task`, alternativa ao `delete_task` que não apaga nada. Tasks arquivadas não aparecem na listagem padrão. |
| **Métricas Prometheus** | O `/metrics` agora soma todos os processos da API. Antes cada processo mostrava só os próprios números. |
| **Deduplicação (modo relatório)** | Relatório e script para calibrar o limiar do dedup **antes** de ativá-lo, sem alterar memórias. |
| **Rerank opcional** | Reordenação dos resultados de busca por um modelo local, **desligada por padrão**. Para ativar, é preciso gerar a imagem com o modelo (veja o runbook `rerank.md`). |
| **Auditoria do Kanban** | Novo script, só de leitura, que verifica o isolamento por grupo no Planka. |

---

## Cuidados no deploy

1. Fazer backup (`POST /admin/backup/run`) e anotar o `points_count` do Qdrant.
2. **Planka:** rodar o script de auditoria. Se ele listar projetos em que o usuário admin do espelho não é gerente, aplicar o SQL do runbook `kanban-group-visibility.md`.
3. Rebuild **apenas** de `openmemory-mcp`, `openmemory-write-worker`, `openmemory-ui`, `planka` **e** `openmemory-governance-worker`.
4. **Nunca** usar `down -v` e **não** recriar o `mem0_store` (Qdrant).
5. A governança continua **pausada** até a validação pós-deploy, e só deve ser reativada depois que o `openmemory-governance-worker` estiver rodando a imagem nova.
6. Depois do deploy, conferir que o `points_count` não mudou, que `/health` está ok e que a migration aplicada é a `t1u2v3w4x5y6`.

As instruções completas estão no PR #39.

---

## Planos de testes manuais

- Log de acesso: `openmemory/docs/test-plans/01ada614-access-log.md`
- Card do Planka vira task: `openmemory/docs/test-plans/ab0b1e8d-planka-card-import.md`
- Merge de projetos: `openmemory/docs/test-plans/8e45acf8-project-merge.md`
- Quadros por grupo: `openmemory/docs/test-plans/2feaabe4-kanban-group-visibility.md`

## Próximos passos (fora desta liberação)

- **Backfill do Planka:** importar, pelo admin, cards antigos ou cards cujo aviso de criação se perdeu. Até lá, um card criado no Planka enquanto a integração estiver fora do ar não vira task automaticamente.
- Reforço de segurança do token e dos IDs da bridge entre ShareMem e Planka.
- Log de acesso: exibir os acessos MCP sem identificação confirmada sem atribuí-los a uma pessoa.
- Estatísticas de acesso por pessoa: hoje as leituras feitas pela UI são creditadas ao dono da máquina.
