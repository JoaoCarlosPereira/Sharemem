import {
  CLEARED_SECRET,
  isMaskedSecret,
  nextSecretInput,
  preserveMaskedSecrets,
} from "@/lib/secret-mask";

describe("secret-mask", () => {
  it("reconhece o formato de máscara da API", () => {
    expect(isMaskedSecret("****abcd")).toBe(true);
    expect(isMaskedSecret("****")).toBe(true);
    expect(isMaskedSecret("env:OPENAI_API_KEY")).toBe(false);
    expect(isMaskedSecret("sk-123")).toBe(false);
    expect(isMaskedSecret(undefined)).toBe(false);
  });

  it("valor não mascarado: repassa o que foi digitado", () => {
    expect(nextSecretInput("sk-old", "sk-old2")).toBe("sk-old2");
    expect(nextSecretInput(undefined, "x")).toBe("x");
  });

  it("digitar após a máscara mantém só o texto novo (não reenvia máscara)", () => {
    expect(nextSecretInput("****abcd", "****abcdN")).toBe("N");
  });

  it("apagar parte da máscara limpa o campo", () => {
    expect(nextSecretInput("****abcd", "****abc")).toBe("");
    expect(nextSecretInput("****abcd", "***")).toBe("");
  });

  it("colar valor novo sobre a máscara (seleção total) usa o valor novo", () => {
    expect(nextSecretInput("****abcd", "sk-new-key")).toBe("sk-new-key");
    expect(nextSecretInput("****abcd", "env:LLM_API_KEY")).toBe("env:LLM_API_KEY");
  });

  it("máscara inalterada continua a mesma (API preserva o segredo real)", () => {
    expect(nextSecretInput("****abcd", "****abcd")).toBe("****abcd");
  });

  describe("preserveMaskedSecrets (M3: backspace sobre a máscara não apaga o segredo)", () => {
    const loaded = {
      mem0: {
        llm: { provider: "openai", config: { model: "m", api_key: "****abcd" } },
        embedder: { provider: "openai", config: { model: "e", api_key: "env:X" } },
        vector_store: { config: { hosts: ["****9200", "http://b"] } },
      },
    };

    it("'' num campo que veio mascarado volta a ser a máscara (API mantém o real)", () => {
      const typed = nextSecretInput("****abcd", "****abc"); // backspace
      expect(typed).toBe("");
      const next = {
        mem0: { ...loaded.mem0, llm: { provider: "openai", config: { model: "m2", api_key: typed } } },
      };
      const out = preserveMaskedSecrets(next, loaded);
      expect(out.mem0.llm.config.api_key).toBe("****abcd");
      expect(out.mem0.llm.config.model).toBe("m2");
    });

    it("'' em campo que NÃO era mascarado continua ''", () => {
      const next = { mem0: { embedder: { config: { api_key: "" } } } };
      expect(preserveMaskedSecrets(next, loaded).mem0.embedder.config.api_key).toBe("");
    });

    it("valor novo digitado é enviado como está", () => {
      const next = { mem0: { llm: { config: { api_key: "sk-nova" } } } };
      expect(preserveMaskedSecrets(next, loaded).mem0.llm.config.api_key).toBe("sk-nova");
    });

    it("ação explícita de remover (null) não é revertida", () => {
      const next = { mem0: { llm: { config: { api_key: CLEARED_SECRET } } } };
      expect(preserveMaskedSecrets(next, loaded).mem0.llm.config.api_key).toBeNull();
    });

    it("percorre listas por índice e não muta a entrada", () => {
      const next = { mem0: { vector_store: { config: { hosts: ["", "http://b"] } } } };
      const out = preserveMaskedSecrets(next, loaded);
      expect(out.mem0.vector_store.config.hosts).toEqual(["****9200", "http://b"]);
      expect(next.mem0.vector_store.config.hosts[0]).toBe("");
    });
  });
});
