---
name: recall-memoria-mem0
description: Hook UserPromptSubmit que busca no ShareMem as memórias relevantes ao prompt e as injeta no contexto antes da resposta, identificando quem lê pelo token de agente e não pela máquina.
---

# Recall automático de memórias (ShareMem)

A cada prompt, o hook consulta `POST /v3/memories/search/` no servidor do ShareMem
e injeta até 5 memórias relevantes no contexto, num bloco `<mem0-memories>`.

## Comportamento

- **Código de tarefa** (6 dígitos, com ou sem `#`): faz uma busca dedicada e só
  aproveita memórias que citam o código literalmente. Esses resultados vêm antes
  dos semânticos, sem aumentar o total injetado.
- **Slash-command sem texto livre** (ex.: `/tarefa 371145`): não faz a busca
  semântica genérica, que só traria vizinhos recentes e irrelevantes.
- **Score mínimo** de 0,3 para resultados semânticos.
- **Timeout** de 2,5 s; qualquer falha (rede, servidor, JSON) termina em silêncio
  com exit 0. O prompt nunca é bloqueado.

## Identidade: token de agente

O hook lê o token de agente (`omtk_…`) do parâmetro `?token=` da URL do MCP
`mem0` (ou `sharemem`) em `~/.claude.json` e envia `Authorization: Bearer <token>`.
Com isso o servidor atribui a leitura à pessoa dona do token, independente da
máquina. Sem o token, a leitura é atribuída ao hostname da estação, e uma máquina
nova, ainda não vinculada, aparece sem nome no ranking de contribuidores.

O token não é copiado para nenhum outro arquivo.

Requer o servidor com a correção do `compat_v3` (PR #44). Em servidor anterior o
token é aceito, mas a atribuição continua pelo hostname.

## Requisitos

- `node` 18+ no PATH (usa `fetch` nativo).
- `~/.mem0/settings.json` com `"auto_search": true`; sem isso o hook não faz nada.
  Com `"auto_save": true` ele também acrescenta o lembrete de captura.
- MCP `mem0` ou `sharemem` configurado com `?token=` na URL.
- Servidor em `OPENMEMORY_API_BASE` (padrão `http://192.168.3.213:8765`).

## Migração de quem já tem o hook local

Quem instalou o hook antes por cópia manual tem no `~/.claude/settings.json` uma
entrada `UserPromptSubmit` com `node ".../.claude/mem0/mem0-hook.mjs"`. **Remova
essa entrada** ao instalar este pacote, ou as memórias serão injetadas duas vezes.

## Testes

```bash
node --test mem0-hook.test.mjs
```
