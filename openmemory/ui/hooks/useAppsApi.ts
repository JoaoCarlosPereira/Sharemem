import { useState, useCallback } from 'react';
import axios from 'axios';
import { useDispatch, useSelector } from 'react-redux';
import { AppDispatch, RootState } from '@/store/store';
import {
  App,
  AppDetails,
  AppMemory,
  AccessedMemory,
  setAppsSuccess,
  setAppsError,
  setAppsLoading,
  setSelectedAppLoading,
  setSelectedAppDetails,
  setCreatedMemoriesLoading,
  setCreatedMemoriesSuccess,
  setCreatedMemoriesError,
  setAccessedMemoriesLoading,
  setAccessedMemoriesSuccess,
  setAccessedMemoriesError,
  setSelectedAppError,
} from '@/store/appsSlice';
import { getApiUrl } from "@/lib/api-url";
import { parseApiError } from "@/lib/api-errors";

interface ApiResponse {
  total: number;
  total_memories_created?: number;
  page: number;
  page_size: number;
  apps: App[];
}

interface MemoriesResponse {
  total: number;
  page: number;
  page_size: number;
  memories: AppMemory[];
}

interface AccessedMemoriesResponse {
  total: number;
  page: number;
  page_size: number;
  memories: AccessedMemory[];
}

interface FetchAppsParams {
  name?: string;
  is_active?: boolean;
  sort_by?: 'name' | 'memories' | 'memories_accessed';
  sort_direction?: 'asc' | 'desc';
  page?: number;
  page_size?: number;
}

/**
 * ``POST /api/v1/apps/{id}/rename``: 200 aplica o rename; 202 ``proposal_pending``
 * quando o destino já existe (vira proposta de unificação em Governança).
 */
export type RenameProjectResult =
  | { status: "success"; new_name?: string; moved_memories?: number; message?: string }
  | { status: "proposal_pending"; new_name: string; message?: string; proposal?: { id: string } | null };

interface UseAppsApiReturn {
  fetchApps: (params?: FetchAppsParams) => Promise<{ apps: App[], total: number }>;
  fetchAppDetails: (appId: string) => Promise<void>;
  fetchAppMemories: (appId: string, page?: number, pageSize?: number) => Promise<void>;
  fetchAppAccessedMemories: (appId: string, page?: number, pageSize?: number) => Promise<void>;
  updateAppDetails: (appId: string, details: { is_active: boolean }) => Promise<void>;
  renameApp: (appId: string, newName: string) => Promise<RenameProjectResult>;
  deleteApp: (appId: string, confirmName: string) => Promise<{ deleted_memories: number; project: string }>;
  isLoading: boolean;
  error: string | null;
}

export const useAppsApi = (): UseAppsApiReturn => {
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const dispatch = useDispatch<AppDispatch>();
  const user_id = useSelector((state: RootState) => state.profile.userId);

  
  const fetchApps = useCallback(async (params: FetchAppsParams = {}): Promise<{ apps: App[], total: number }> => {
    const {
      name,
      is_active,
      sort_by = 'memories',
      sort_direction = 'desc',
      page = 1,
      page_size = 100,
    } = params;

    setIsLoading(true);
    dispatch(setAppsLoading());
    try {
      const queryParams = new URLSearchParams({
        page: String(page),
        page_size: String(page_size)
      });

      if (name) queryParams.append('name', name);
      if (is_active !== undefined) queryParams.append('is_active', String(is_active));
      if (sort_by) queryParams.append('sort_by', sort_by);
      if (sort_direction) queryParams.append('sort_direction', sort_direction);

      const response = await axios.get<ApiResponse>(
        `${getApiUrl()}/api/v1/apps/?${queryParams.toString()}`
      );

      const totalMemoriesCreated =
        response.data.total_memories_created ??
        response.data.apps.reduce((sum, app) => sum + app.total_memories_created, 0);

      setIsLoading(false);
      dispatch(
        setAppsSuccess({
          apps: response.data.apps,
          total: response.data.total,
          totalMemoriesCreated,
        }),
      );
      return {
        apps: response.data.apps,
        total: response.data.total,
      };
    } catch (err: any) {
      const errorMessage = err.message || 'Falha ao buscar projetos';
      setError(errorMessage);
      dispatch(setAppsError(errorMessage));
      setIsLoading(false);
      throw new Error(errorMessage);
    }
  }, [dispatch]);

  const fetchAppDetails = useCallback(async (appId: string): Promise<void> => {
    setIsLoading(true);
    dispatch(setSelectedAppLoading());
    try {
      const response = await axios.get<AppDetails>(
        `${getApiUrl()}/api/v1/apps/${appId}`
      );
      dispatch(setSelectedAppDetails(response.data));
      setIsLoading(false);
    } catch (err: any) {
      const errorMessage = err.message || 'Falha ao buscar detalhes do app';
      dispatch(setSelectedAppError(errorMessage));
      setError(errorMessage);
      setIsLoading(false);
      throw new Error(errorMessage);
    }
  }, [dispatch]);

  const fetchAppMemories = useCallback(async (appId: string, page: number = 1, pageSize: number = 10): Promise<void> => {
    setIsLoading(true);
    dispatch(setCreatedMemoriesLoading());
    try {
      const response = await axios.get<MemoriesResponse>(
        `${getApiUrl()}/api/v1/apps/${appId}/memories?page=${page}&page_size=${pageSize}`
      );
      dispatch(setCreatedMemoriesSuccess({
        items: response.data.memories,
        total: response.data.total,
        page: response.data.page,
      }));
      setIsLoading(false);
    } catch (err: any) {
      const errorMessage = err.message || 'Falha ao buscar memórias do app';
      dispatch(setCreatedMemoriesError(errorMessage));
      setError(errorMessage);
      setIsLoading(false);
    }
  }, [dispatch]);

  const fetchAppAccessedMemories = useCallback(async (appId: string, page: number = 1, pageSize: number = 10): Promise<void> => {
    setIsLoading(true);
    dispatch(setAccessedMemoriesLoading());
    try {
      const response = await axios.get<AccessedMemoriesResponse>(
        `${getApiUrl()}/api/v1/apps/${appId}/accessed?page=${page}&page_size=${pageSize}`
      );
      dispatch(setAccessedMemoriesSuccess({
        items: response.data.memories,
        total: response.data.total,
        page: response.data.page,
      }));
      setIsLoading(false);
    } catch (err: any) {
      const errorMessage = err.message || 'Falha ao buscar memórias acessadas';
      dispatch(setAccessedMemoriesError(errorMessage));
      setError(errorMessage);
      setIsLoading(false);
    }
  }, [dispatch]);

  const updateAppDetails = async (appId: string, details: { is_active: boolean }) => {
    setIsLoading(true);
    try {
      const response = await axios.put(
        `${getApiUrl()}/api/v1/apps/${appId}?is_active=${details.is_active}`
      );
      setIsLoading(false);
      return response.data;
    } catch (error) {
      console.error("Failed to update app details:", error);
      setIsLoading(false);
      throw error;
    }
  };

  const renameApp = async (appId: string, newName: string) => {
    setIsLoading(true);
    try {
      const response = await axios.post<RenameProjectResult>(`${getApiUrl()}/api/v1/apps/${appId}/rename`, {
        new_name: newName,
      });
      return response.data;
    } catch (error) {
      if (axios.isAxiosError(error)) {
        throw new Error(error.response?.data?.detail || "Failed to rename project");
      }
      throw error;
    } finally {
      setIsLoading(false);
    }
  };

  const deleteApp = async (appId: string, confirmName: string) => {
    setIsLoading(true);
    try {
      const response = await axios.post<{
        deleted_memories: number;
        project: string;
        message: string;
      }>(`${getApiUrl()}/api/v1/apps/${appId}/actions/delete`, {
        confirm_name: confirmName,
        user_id,
      });
      setIsLoading(false);
      return {
        deleted_memories: response.data.deleted_memories,
        project: response.data.project,
      };
    } catch (err: unknown) {
      const errorMessage = parseApiError(err, "Falha ao excluir projeto");
      setError(errorMessage);
      setIsLoading(false);
      throw new Error(errorMessage);
    }
  };

  return {
    fetchApps,
    fetchAppDetails,
    fetchAppMemories,
    fetchAppAccessedMemories,
    updateAppDetails,
    renameApp,
    deleteApp,
    isLoading,
    error
  };
};
