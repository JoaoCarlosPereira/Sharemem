import { useState } from 'react';
import { apiClient } from '@/lib/api-client';
import { useDispatch, useSelector } from 'react-redux';
import { AppDispatch, RootState } from '@/store/store';
import {
  setConfigLoading,
  setConfigSuccess,
  setConfigError,
  updateLLM,
  updateEmbedder,
  updateMem0Config,
  updateOpenMemory,
  LLMProvider,
  EmbedderProvider,
  Mem0Config,
  OpenMemoryConfig
} from '@/store/configSlice';
import { getApiUrl } from "@/lib/api-url";
import { preserveMaskedSecrets } from "@/lib/secret-mask";

/**
 * ``/api/v1/config`` exige admin (``require_admin``) e devolve segredos
 * mascarados. Usa o ``apiClient`` (Bearer da sessão); em modo legado o
 * ``/api-proxy`` injeta ``X-Admin-Token`` server-side.
 *
 * Antes de salvar, ``preserveMaskedSecrets`` troca ``""`` (máscara apagada no
 * formulário) pela máscara original: a API mantém o segredo em vez de apagá-lo.
 */
const CONFIG_API = () => `${getApiUrl()}/api/v1/config`;

interface UseConfigApiReturn {
  fetchConfig: () => Promise<void>;
  saveConfig: (config: { openmemory?: OpenMemoryConfig; mem0: Mem0Config }) => Promise<void>;
  saveLLMConfig: (llmConfig: LLMProvider) => Promise<void>;
  saveEmbedderConfig: (embedderConfig: EmbedderProvider) => Promise<void>;
  resetConfig: () => Promise<void>;
  isLoading: boolean;
  error: string | null;
}

export const useConfig = (): UseConfigApiReturn => {
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const dispatch = useDispatch<AppDispatch>();
  const loadedConfig = useSelector((state: RootState) => state.config);
    
  const fetchConfig = async () => {
    setIsLoading(true);
    dispatch(setConfigLoading());
    
    try {
      const response = await apiClient.get(CONFIG_API());
      dispatch(setConfigSuccess(response.data));
      setIsLoading(false);
    } catch (err: any) {
      const errorMessage = err.response?.data?.detail || err.message || 'Falha ao buscar configuração';
      dispatch(setConfigError(errorMessage));
      setError(errorMessage);
      setIsLoading(false);
      throw new Error(errorMessage);
    }
  };

  const saveConfig = async (config: { openmemory?: OpenMemoryConfig; mem0: Mem0Config }) => {
    setIsLoading(true);
    setError(null);
    
    try {
      const response = await apiClient.put(CONFIG_API(), preserveMaskedSecrets(config, loadedConfig));
      dispatch(setConfigSuccess(response.data));
      setIsLoading(false);
      return response.data;
    } catch (err: any) {
      const errorMessage = err.response?.data?.detail || err.message || 'Falha ao salvar configuração';
      dispatch(setConfigError(errorMessage));
      setError(errorMessage);
      setIsLoading(false);
      throw new Error(errorMessage);
    }
  };

  const resetConfig = async () => {
    setIsLoading(true);
    setError(null);
    
    try {
      const response = await apiClient.post(`${getApiUrl()}/api/v1/config/reset`);
      dispatch(setConfigSuccess(response.data));
      setIsLoading(false);
      return response.data;
    } catch (err: any) {
      const errorMessage = err.response?.data?.detail || err.message || 'Falha ao redefinir configuração';
      dispatch(setConfigError(errorMessage));
      setError(errorMessage);
      setIsLoading(false);
      throw new Error(errorMessage);
    }
  };

  const saveLLMConfig = async (llmConfig: LLMProvider) => {
    setIsLoading(true);
    setError(null);
    
    try {
      const response = await apiClient.put(
        `${getApiUrl()}/api/v1/config/mem0/llm`,
        preserveMaskedSecrets(llmConfig, loadedConfig.mem0?.llm),
      );
      dispatch(updateLLM(response.data));
      setIsLoading(false);
      return response.data;
    } catch (err: any) {
      const errorMessage = err.response?.data?.detail || err.message || 'Falha ao salvar configuração do LLM';
      setError(errorMessage);
      setIsLoading(false);
      throw new Error(errorMessage);
    }
  };

  const saveEmbedderConfig = async (embedderConfig: EmbedderProvider) => {
    setIsLoading(true);
    setError(null);
    
    try {
      const response = await apiClient.put(
        `${getApiUrl()}/api/v1/config/mem0/embedder`,
        preserveMaskedSecrets(embedderConfig, loadedConfig.mem0?.embedder),
      );
      dispatch(updateEmbedder(response.data));
      setIsLoading(false);
      return response.data;
    } catch (err: any) {
      const errorMessage = err.response?.data?.detail || err.message || 'Falha ao salvar configuração do embedder';
      setError(errorMessage);
      setIsLoading(false);
      throw new Error(errorMessage);
    }
  };

  return {
    fetchConfig,
    saveConfig,
    saveLLMConfig,
    saveEmbedderConfig,
    resetConfig,
    isLoading,
    error
  };
}; 