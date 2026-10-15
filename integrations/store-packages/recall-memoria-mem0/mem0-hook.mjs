#!/usr/bin/env node
// mem0 global hook (UserPromptSubmit) — pacote recall-memoria-mem0 da Store do ShareMem.
// Lê o modo em ~/.mem0/settings.json e, se auto_search, injeta memórias relevantes no contexto.
// Falha SEMPRE em silêncio (exit 0) para nunca bloquear o prompt.
//
// Recall (2026-09-01): a busca puramente semântica não distingue código de tarefa.
// Medido no acervo: a consulta "371145" produz scores entre 0,511 e 0,577 em 100
// candidatos — amplitude 0,066, ou seja, ruído. Quando o prompt traz um código de
// tarefa, o hook faz uma busca dedicada e filtra por ocorrência LITERAL do código;
// e quando o prompt é só um slash-command (sem texto livre), a busca semântica
// genérica é suprimida em vez de injetar 5 memórias irrelevantes.

import { readFileSync } from "node:fs";
import { homedir, hostname } from "node:os";
import { join, basename } from "node:path";
import { pathToFileURL } from "node:url";

const BASE =
  process.env.OPENMEMORY_API_BASE?.replace(/\/+$/, "") ||
  "http://192.168.3.213:8765";
const TIMEOUT_MS = 2500;
const LIMIT = 5;
const MIN_SCORE = 0.3;
// Pool da busca por código: o código não tem sinal semântico, então recuperamos
// candidatos e filtramos por ocorrência literal. 60 é o suficiente para cobrir o
// acervo de uma tarefa sem estourar o timeout.
const TASK_FETCH = 60;

const TASK_CODE_RE = /(?<!\d)#?(\d{6})(?!\d)/g;
const SLASH_COMMAND_RE = /^\/[\w:-]+/;

/** Códigos de tarefa (6 dígitos, com ou sem `#`) citados no prompt, sem repetição. */
export function extractTaskCodes(prompt) {
  const seen = new Set();
  for (const m of String(prompt || "").matchAll(TASK_CODE_RE)) seen.add(m[1]);
  return [...seen].slice(0, 2);
}

/**
 * Texto livre do prompt: sem o slash-command inicial e sem os códigos de tarefa.
 * É o que sobra de sinal semântico depois de tirar o que já é chave exata.
 */
export function freeText(prompt) {
  return String(prompt || "")
    .replace(SLASH_COMMAND_RE, " ")
    .replace(TASK_CODE_RE, " ")
    .replace(/\s+/g, " ")
    .trim();
}

/**
 * True quando o texto livre não sustenta uma busca semântica — caso do
 * `/tarefa 371145`, em que sobra string vazia. Buscar assim devolve os vizinhos
 * mais recentes do acervo, não os relevantes.
 */
export function isLowSignal(prompt) {
  const words = freeText(prompt)
    .split(/[^\p{L}\p{N}_.-]+/u)
    .filter((w) => w.replace(/[^\p{L}]/gu, "").length >= 3);
  return words.length < 3;
}

/** Uma memória "bate" com o código quando ele aparece literal no texto ou no projeto. */
export function matchesTaskCode(result, codes) {
  const hay = `${result?.memory ?? ""} ${result?.project ?? ""} ${
    result?.metadata?.project ?? ""
  }`;
  return codes.some((c) => hay.includes(c));
}

/**
 * Monta a lista final: os acertos por código de tarefa vêm ANTES dos semânticos,
 * sem aumentar o total injetado (o teto continua sendo LIMIT).
 */
export function mergeResults(taskHits, semanticHits, codes, limit = LIMIT) {
  const out = [];
  const seen = new Set();
  const push = (r) => {
    if (!r?.memory) return;
    const key = r.id ?? r.memory;
    if (seen.has(key) || out.length >= limit) return;
    seen.add(key);
    out.push(r);
  };
  for (const r of taskHits) if (matchesTaskCode(r, codes)) push(r);
  for (const r of semanticHits) if ((r.score ?? 0) >= MIN_SCORE) push(r);
  return out;
}

function readJson(path) {
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch {
    return null;
  }
}

/**
 * Token de agente (``omtk_``) da URL do MCP mem0 em ~/.claude.json.
 * Identifica a pessoa independente da máquina: sem ele o servidor atribui a
 * leitura ao ``user_id`` (hostname) e uma máquina não vinculada aparece anônima.
 */
export function agentToken(claudeJson) {
  const url = claudeJson?.mcpServers?.mem0?.url ?? claudeJson?.mcpServers?.sharemem?.url;
  if (typeof url !== "string") return null;
  try {
    const token = new URL(url).searchParams.get("token");
    return token && token.startsWith("omtk_") ? token : null;
  } catch {
    return null;
  }
}

const TOKEN = agentToken(readJson(join(homedir(), ".claude.json")));

function readStdin() {
  try {
    return readFileSync(0, "utf8");
  } catch {
    return "";
  }
}

async function search(body, signal) {
  try {
    const res = await fetch(`${BASE}/v3/memories/search/`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(TOKEN ? { Authorization: `Bearer ${TOKEN}` } : {}),
      },
      body: JSON.stringify(body),
      signal,
    });
    if (!res.ok) return [];
    const data = await res.json();
    return Array.isArray(data?.results) ? data.results : [];
  } catch {
    return []; // rede/timeout: silencioso
  }
}

async function main() {
  const settings = readJson(join(homedir(), ".mem0", "settings.json")) || {};
  // Sem busca automática => no-op (modos read-only/read-write ativam; manual desativa).
  if (settings.auto_search !== true) return;

  const input = safeParse(readStdin()) || {};
  const prompt = (input.prompt || "").trim();
  if (!prompt) return;

  const cwd = input.cwd || process.cwd();
  let project = basename(cwd) || "mem0-shared";
  if (cwd === homedir()) project = "mem0-shared";

  const codes = extractTaskCodes(prompt);
  const lowSignal = isLowSignal(prompt);

  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
  let taskHits = [];
  let semanticHits = [];
  try {
    const jobs = [];
    if (codes.length) {
      // Consulta enriquecida: "TAREFA #371145 371145 <texto livre>" recupera o
      // bairro certo do espaço vetorial; o filtro literal faz a seleção.
      const q = `${codes.map((c) => `TAREFA #${c} ${c}`).join(" ")} ${freeText(
        prompt
      )}`.trim();
      jobs.push(
        search(
          { query: q, user_id: hostname(), project, limit: TASK_FETCH },
          ctrl.signal
        ).then((r) => {
          taskHits = r;
        })
      );
    }
    // Prompt sem texto livre (slash-command puro) não sustenta busca semântica:
    // trocar ruído por nada é ganho, memória irrelevante custa contexto e induz erro.
    if (!lowSignal) {
      jobs.push(
        search(
          { query: prompt, user_id: hostname(), project, limit: LIMIT },
          ctrl.signal
        ).then((r) => {
          semanticHits = r;
        })
      );
    }
    await Promise.all(jobs);
  } finally {
    clearTimeout(t);
  }

  const top = mergeResults(taskHits, semanticHits, codes, LIMIT);

  const lines = [];
  if (top.length) {
    const scope = codes.length ? ` tarefa="${codes.join(",")}"` : "";
    lines.push(`<mem0-memories project="${project}"${scope} host="${hostname()}">`);
    for (const r of top) {
      const proj = r.metadata?.project ? ` [${r.metadata.project}]` : "";
      lines.push(`- ${String(r.memory).replace(/\s+/g, " ").trim()}${proj}`);
    }
    lines.push("</mem0-memories>");
    lines.push(
      "Memórias recuperadas do mem0 (servidor LOCAL). Use o que for relevante; ignore o resto."
    );
  }
  if (settings.auto_save === true) {
    lines.push(
      "Modo de captura ATIVO: ao concluir algo durável (decisão/preferência/aprendizado), chame a ferramenta MCP mem0 add_memory com 1 frase objetiva."
    );
  }

  if (!lines.length) return;

  const out = {
    hookSpecificOutput: {
      hookEventName: "UserPromptSubmit",
      additionalContext: lines.join("\n"),
    },
  };
  process.stdout.write(JSON.stringify(out));
}

function safeParse(s) {
  try {
    return JSON.parse(s);
  } catch {
    return null;
  }
}

// Só executa quando este arquivo É o processo; sob `node --test` ele é apenas
// importado e as funções puras acima ficam disponíveis sem tocar em stdin.
const isEntrypoint =
  process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (isEntrypoint) {
  main().catch(() => {}).finally(() => process.exit(0));
}
