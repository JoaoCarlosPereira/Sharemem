import {
  PACKAGED_KINDS,
  REGISTRY_KIND_API_KIND,
  REGISTRY_KIND_LABELS,
  REGISTRY_RESOURCE_KINDS,
  parseHookEvents,
  validatePublishDraft,
  type PublishDraft,
} from "@/lib/registry-client";

const draft = (overrides: Partial<PublishDraft> = {}): PublishDraft => ({
  kind: "hooks",
  name: "bloqueio-db",
  tag: "latest",
  title: "Bloqueio de banco externo",
  description: "Impede conexao fora da rede local",
  sourceRepository: "",
  promptContent: "",
  skillContent: "",
  hookContent: "# Hook\n\nBloqueia conexoes externas.",
  hookEvents: JSON.stringify({
    PreToolUse: [{ matcher: "Bash", hooks: [{ type: "command", command: "echo oi" }] }],
  }),
  ...overrides,
});

describe("catálogo de hooks", () => {
  it("expõe hooks como tipo de recurso empacotado", () => {
    expect(REGISTRY_RESOURCE_KINDS).toContain("hooks");
    expect(REGISTRY_KIND_LABELS.hooks).toBe("Hooks");
    expect(REGISTRY_KIND_API_KIND.hooks).toBe("Hook");
    expect(PACKAGED_KINDS).toEqual(["skills", "hooks"]);
  });

  it("aceita um mapa de eventos válido", () => {
    const parsed = parseHookEvents(draft().hookEvents);
    expect(parsed).toEqual({
      events: {
        PreToolUse: [{ matcher: "Bash", hooks: [{ type: "command", command: "echo oi" }] }],
      },
    });
  });

  it.each([
    ["{", "JSON válido"],
    ["[]", "objeto evento"],
    ["{}", "ao menos um evento"],
    ['{"PreToolUse": []}', "grupo de matcher"],
    ['{"PreToolUse": [{"hooks": []}]}', 'sem entradas em "hooks"'],
  ])("rejeita mapa de eventos %s", (text, message) => {
    const parsed = parseHookEvents(text);
    expect("error" in parsed && parsed.error).toEqual(expect.stringContaining(message));
  });

  it("exige documentação e eventos ao publicar um hook", () => {
    expect(validatePublishDraft(draft())).toEqual([]);
    expect(validatePublishDraft(draft({ hookContent: "" }))).toEqual([
      expect.stringContaining("HOOK.md"),
    ]);
    expect(validatePublishDraft(draft({ hookEvents: "{}" }))).toEqual([
      expect.stringContaining("ao menos um evento"),
    ]);
  });

  it("dispensa HOOK.md inline quando a pasta completa foi selecionada", () => {
    expect(validatePublishDraft(draft({ hookContent: "" }), true)).toEqual([]);
  });
});
