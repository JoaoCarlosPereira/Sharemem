// node --test  (roda na pasta do hook)
// Falha contra a versão anterior do hook: ela não tinha nenhuma dessas funções e
// mandava o prompt cru como query, sem tratar código de tarefa nem slash-command.
import test from "node:test";
import assert from "node:assert/strict";
import {
  agentToken,
  extractTaskCodes,
  freeText,
  isLowSignal,
  matchesTaskCode,
  mergeResults,
} from "./mem0-hook.mjs";

test("extrai código de tarefa com e sem #", () => {
  assert.deepEqual(extractTaskCodes("/tarefa 371145"), ["371145"]);
  assert.deepEqual(extractTaskCodes("ver #371145 agora"), ["371145"]);
  assert.deepEqual(extractTaskCodes("371145 e 372244"), ["371145", "372244"]);
});

test("não confunde versão nem número de 5 ou 7 dígitos com tarefa", () => {
  assert.deepEqual(extractTaskCodes("versão 2.89.24"), []);
  assert.deepEqual(extractTaskCodes("id 1234567"), []);
  assert.deepEqual(extractTaskCodes("porta 9443"), []);
});

test("slash-command com código é prompt de sinal baixo", () => {
  assert.equal(freeText("/tarefa 371145"), "");
  assert.equal(isLowSignal("/tarefa 371145"), true);
  assert.equal(isLowSignal("/tarefa 371145 revisar as alíquotas efetivas"), false);
});

test("prompt com texto livre real não é suprimido", () => {
  assert.equal(isLowSignal("como está a entrega das alíquotas efetivas"), false);
});

test("acerto por código exige ocorrência literal", () => {
  const codes = ["371145"];
  assert.equal(matchesTaskCode({ memory: "TAREFA #371145 liberada" }, codes), true);
  assert.equal(matchesTaskCode({ memory: "nada a ver", project: "371145" }, codes), true);
  assert.equal(matchesTaskCode({ memory: "TAREFA #372244" }, codes), false);
});

test("acertos por código vêm antes dos semânticos e o teto não aumenta", () => {
  const codes = ["371145"];
  const taskHits = [
    { id: "a", memory: "decisão de 28/08 na TAREFA #371145", score: 0.55 },
    { id: "ruido", memory: "outra coisa qualquer", score: 0.54 },
    { id: "b", memory: "MR da 371145 aceito", score: 0.52 },
  ];
  const semanticHits = [
    { id: "c", memory: "planejamento de 06/08", score: 0.81 },
    { id: "d", memory: "planejamento de 19/08", score: 0.80 },
    { id: "e", memory: "coisa de outro projeto", score: 0.79 },
    { id: "f", memory: "mais uma", score: 0.78 },
    { id: "g", memory: "e mais uma", score: 0.77 },
  ];
  const out = mergeResults(taskHits, semanticHits, codes, 5);
  assert.equal(out.length, 5);
  assert.deepEqual(out.slice(0, 2).map((r) => r.id), ["a", "b"]);
  assert.equal(out.some((r) => r.id === "ruido"), false);
});

test("sem código, o comportamento semântico continua o mesmo", () => {
  const semanticHits = [
    { id: "c", memory: "alta", score: 0.81 },
    { id: "d", memory: "baixa demais", score: 0.10 },
  ];
  const out = mergeResults([], semanticHits, [], 5);
  assert.deepEqual(out.map((r) => r.id), ["c"]);
});

test("dedupe entre as duas buscas não gasta slot duas vezes", () => {
  const codes = ["371145"];
  const shared = { id: "x", memory: "TAREFA #371145 entregue", score: 0.6 };
  const out = mergeResults([shared], [shared], codes, 5);
  assert.equal(out.length, 1);
});

test("token de agente vem da URL do MCP mem0 no ~/.claude.json", () => {
  const url = "http://192.168.3.213:8765/mcp/claude-code/http/S0360?token=omtk_abc123&group=Fiscal";
  assert.equal(agentToken({ mcpServers: { mem0: { url } } }), "omtk_abc123");
  assert.equal(agentToken({ mcpServers: { sharemem: { url } } }), "omtk_abc123");
});

test("sem token de agente o hook segue sem Authorization", () => {
  assert.equal(agentToken(null), null);
  assert.equal(agentToken({ mcpServers: {} }), null);
  assert.equal(agentToken({ mcpServers: { mem0: { url: "http://h/mcp/x/http/S0360?group=Fiscal" } } }), null);
  assert.equal(agentToken({ mcpServers: { mem0: { url: "http://h/mcp?token=local" } } }), null);
  assert.equal(agentToken({ mcpServers: { mem0: { url: "nao e url" } } }), null);
});
