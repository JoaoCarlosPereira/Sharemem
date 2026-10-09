# ADR-009: Hooks como tipo de primeira classe da Store

**Status**: Aceito  
**Data**: 2026-09-12

## Contexto

Hooks de ciclo de vida (PreToolUse, SessionStart, …) já circulavam na equipe, mas
a Store não tinha tipo para eles. O contorno em uso era publicar o hook como
**Skill** (`default/hooks-memoria-mem0`), o que traz três problemas: o hook aparece
na aba errada, nada valida o formato dos eventos, e a receita de instalação copia
uma pasta em vez de escrever no `settings.json` — quem instala precisa editar o
arquivo à mão.

O AgentRegistry já modela hooks internamente: `HookMatcherGroup` / `HookEntry`
(`command|prompt|agent|http|mcp_tool`) são usados dentro do manifesto de Plugin.
Faltava expor isso como recurso publicável isolado.

## Decisão

1. **Kind `Hook` nativo** no AgentRegistry, reusando `HookMatcherGroup`/`HookEntry`.
   `HookSpec` = título, descrição, idioma e `events` (mapa evento → grupos).
   Tabela `hooks` + `hook_artifacts` (migração `014`), espelhando `plugins` e
   `skill_artifacts`.
2. **Pacote de arquivos**, como nas Skills: o Hook publica um diretório com
   `HOOK.md` obrigatório na raiz e os scripts que executa. O pacote vai para a
   mesma tabela `artifacts` (content-addressed) e é fixado em
   `status.resolvedSource.artifact` no upload.
3. **`${HOOK_DIR}` nos comandos** aponta para a pasta do próprio pacote. A
   publicação recusa comando que referencie arquivo ausente; a receita expande o
   placeholder para o destino real da extração, conhecido só na instalação.
4. **Alvo Claude Code apenas**. A receita extrai o pacote em
   `~/.claude/hooks/<nome>` e faz merge dos eventos em `~/.claude/settings.json`
   (passo `merge_hooks`, entradas substituídas por dono — reinstalar não duplica
   o hook). Cursor e Codex retornam erro explícito: os eventos do Cursor são
   outros e o Codex não tem sistema equivalente.

## Consequências

- Publicar hook não exige mais disfarçá-lo de Skill; o `hooks-memoria-mem0`
  publicado como Skill continua funcionando e pode ser republicado como Hook.
- O `install.py` passa a conferir, após subir o sidecar, se o Registry serve as
  coleções `skills` e `hooks` — imagem antiga do `agentregistry` passa no `/v0/ping`
  mas devolve 404 em `hooks`, e isso antes só apareceria ao publicar.
- Suporte a Cursor/Codex fica em aberto; entra quando houver mapeamento de
  eventos definido, sem mudar o formato publicado.
