/**
 * useConfig usa o apiClient (Bearer da sessão) — /api/v1/config exige admin.
 * O axios cru não passaria o interceptor de sessão dedicado e quebraria com 401.
 */
import React from "react";
import { configureStore } from "@reduxjs/toolkit";
import { Provider } from "react-redux";
import { act, renderHook } from "@testing-library/react";

jest.mock("@/lib/api-client", () => ({
  apiClient: {
    get: jest.fn(),
    post: jest.fn(),
    put: jest.fn(),
  },
}));

jest.mock("axios");

import axios from "axios";
import { apiClient } from "@/lib/api-client";
import configReducer from "@/store/configSlice";
import { useConfig } from "@/hooks/useConfig";

const mockedClient = apiClient as unknown as {
  get: jest.Mock;
  post: jest.Mock;
  put: jest.Mock;
};
const mockedAxios = axios as jest.Mocked<typeof axios>;

const maskedConfig = {
  openmemory: { custom_instructions: null, multilingual: true },
  mem0: {
    llm: {
      provider: "openai",
      config: {
        model: "m",
        temperature: 0.1,
        max_tokens: 2000,
        api_key: "****abcd",
      },
    },
    embedder: { provider: "ollama", config: { model: "nomic" } },
  },
};

function setup() {
  const store = configureStore({ reducer: { config: configReducer } });
  const wrapper = ({ children }: { children: React.ReactNode }) => (
    <Provider store={store}>{children}</Provider>
  );
  const { result } = renderHook(() => useConfig(), { wrapper });
  return { store, result };
}

beforeEach(() => {
  jest.clearAllMocks();
});

describe("useConfig", () => {
  it("fetchConfig usa apiClient (não axios cru) e guarda a api_key mascarada", async () => {
    mockedClient.get.mockResolvedValue({ data: maskedConfig });
    const { store, result } = setup();
    await act(async () => {
      await result.current.fetchConfig();
    });
    expect(mockedClient.get).toHaveBeenCalledWith("/api-proxy/api/v1/config");
    expect(mockedAxios.get).not.toHaveBeenCalled();
    expect(store.getState().config.mem0.llm?.config.api_key).toBe("****abcd");
  });

  it("saveConfig / reset / LLM / embedder usam apiClient", async () => {
    mockedClient.put.mockResolvedValue({ data: maskedConfig });
    mockedClient.post.mockResolvedValue({ data: maskedConfig });
    const { result } = setup();
    await act(async () => {
      await result.current.saveConfig({ mem0: maskedConfig.mem0 });
      await result.current.resetConfig();
    });
    mockedClient.put.mockResolvedValue({ data: maskedConfig.mem0.llm });
    await act(async () => {
      await result.current.saveLLMConfig(maskedConfig.mem0.llm);
    });
    mockedClient.put.mockResolvedValue({ data: maskedConfig.mem0.embedder });
    await act(async () => {
      await result.current.saveEmbedderConfig(maskedConfig.mem0.embedder);
    });
    expect(mockedClient.put).toHaveBeenCalledWith("/api-proxy/api/v1/config", {
      mem0: maskedConfig.mem0,
    });
    expect(mockedClient.post).toHaveBeenCalledWith("/api-proxy/api/v1/config/reset");
    expect(mockedClient.put).toHaveBeenCalledWith(
      "/api-proxy/api/v1/config/mem0/llm",
      maskedConfig.mem0.llm,
    );
    expect(mockedClient.put).toHaveBeenCalledWith(
      "/api-proxy/api/v1/config/mem0/embedder",
      maskedConfig.mem0.embedder,
    );
    expect(mockedAxios.put).not.toHaveBeenCalled();
    expect(mockedAxios.post).not.toHaveBeenCalled();
  });

  it("propaga o detail do 401 da API", async () => {
    mockedClient.get.mockRejectedValue({
      response: { status: 401, data: { detail: "admin credentials required: send X-Admin-Token" } },
    });
    const { store, result } = setup();
    await act(async () => {
      await expect(result.current.fetchConfig()).rejects.toThrow(
        "admin credentials required: send X-Admin-Token",
      );
    });
    expect(store.getState().config.status).toBe("failed");
  });

  it("M3: api_key apagada ('') após GET mascarado é enviada como máscara, não como ''", async () => {
    mockedClient.get.mockResolvedValue({ data: maskedConfig });
    mockedClient.put.mockResolvedValue({ data: maskedConfig });
    const { result } = setup();
    await act(async () => {
      await result.current.fetchConfig();
    });
    const edited = {
      ...maskedConfig.mem0,
      llm: { ...maskedConfig.mem0.llm, config: { ...maskedConfig.mem0.llm.config, api_key: "" } },
    };
    await act(async () => {
      await result.current.saveConfig({ mem0: edited });
    });
    const sent = mockedClient.put.mock.calls[0][1];
    expect(sent.mem0.llm.config.api_key).toBe("****abcd");

    mockedClient.put.mockResolvedValue({ data: maskedConfig.mem0.llm });
    await act(async () => {
      await result.current.saveLLMConfig(edited.llm);
    });
    expect(mockedClient.put.mock.calls[1][1].config.api_key).toBe("****abcd");
  });
});
