---
name: atualizador-store
description: Hook SessionStart que mantém atualizados os hooks e skills instalados pela Store do ShareMem, trocando os arquivos quando sai versão nova e preservando o que o usuário alterou.
---

# Atualização automática da Store (ShareMem)

No início da sessão, o hook confere se os hooks e skills instalados pela Store têm
versão nova publicada e, quando têm, atualiza os arquivos sozinho.

## O que é verificado

- `~/.claude/hooks/<nome>` e `~/.claude/skills/<nome>` que tenham o marcador
  `.sharemem-store.json`, gravado pela receita de instalação da Store.
- Pastas sem marcador com `HOOK.md` ou `SKILL.md`: se existir pacote com o mesmo
  nome na Store **e os arquivos forem idênticos**, a pasta é adotada (o marcador é
  gravado) e passa a ser atualizada. Se forem diferentes, avisa uma vez para
  reinstalar pela Store. Skill ou hook feito à mão, sem par na Store, nunca é tocado.

## Como atualiza

1. Consulta `POST /api/v1/store/updates` uma vez só, com a lista de pacotes da
   máquina, e recebe a receita atual de cada um que existe na Store.
2. Se mudou, baixa o artefato, confere o sha256 e recusa entradas com caminho
   absoluto, `..` ou link.
3. Monta a pasta nova ao lado e troca de uma vez (renomeação); se algo falhar, a
   instalação anterior fica como estava.

Arquivos que **você criou** na pasta do pacote são mantidos. Arquivos do pacote que
**você alterou** (ex.: `config.json` dos bloqueios) também são mantidos, e o hook
avisa quais foram.

## Quando só avisa

- **Hook cuja versão nova muda os eventos** do `settings.json`: os arquivos não são
  trocados e o agente recebe o aviso para reinstalar pela Store. O `settings.json`
  nunca é editado por este hook.
- Instalação antiga sem registro dos arquivos.

Cada aviso aparece uma vez por versão publicada.

## Frequência e limites

- No máximo uma verificação a cada 6 h (`SHAREMEM_STORE_INTERVALO`, em segundos).
  `SHAREMEM_STORE_FORCAR=1` ignora o intervalo.
- Prazo total de 20 s; qualquer falha de rede termina em silêncio, sem atrasar a sessão.
- Uma sessão por vez (trava em `~/.claude/.sharemem-store-update.lock`).
- Envia `X-Hostname` para o rate limit contar por máquina. Se o servidor responder
  429, a rodada para e a verificação não é marcada como feita: a próxima sessão
  continua de onde parou, sem esperar as 6 h.
- Estado em `~/.claude/.sharemem-store-update.json`.

## Servidor e identidade

Usa o servidor e o token de agente da URL do MCP `mem0` (ou `sharemem`) em
`~/.claude.json`; `OPENMEMORY_API_BASE` tem precedência.

## Requisitos

`python` 3.8+ no PATH (só biblioteca padrão).

## Testes

```bash
python testes.py
```
