import { useState, useCallback, useEffect } from 'react';
import axios from 'axios';
import { Memory, Client, Category } from '@/components/types';
import { useDispatch, useSelector } from 'react-redux';
import { AppDispatch, RootState } from '@/store/store';
import { setAccessLogMeta, setAccessLogs, setMemoriesSuccess, setSelectedMemory, setRelatedMemories } from '@/store/memoriesSlice';
import type { AccessChannelFilter, AccessLogResponse } from '@/types/accessLog';
import { toAccessLogMeta } from '@/lib/access-log';
import { getApiUrl } from "@/lib/api-url";
import { parseApiError } from "@/lib/api-errors";

export interface DeletionPolicy {
  memory_delete_allowed: boolean;
  bulk_delete_allowed: boolean;
  message: string;
}

// Define the new simplified memory type
export interface SimpleMemory {
  id: string;
  text: string;
  created_at: string | number;
  state: string;
  categories: string[];
  app_name: string;
  created_by_hostname?: string | null;
  created_by_client?: string | null;
  created_by_display_name?: string | null;
  created_by_avatar_url?: string | null;
  metadata_?: Record<string, unknown>;
}

// Define the shape of the API response item
interface ApiMemoryItem {
  id: string;
  content: string;
  created_at: string;
  state: string;
  app_id: string;
  categories: string[];
  metadata_?: Record<string, any>;
  app_name: string;
  group?: string | null;
  created_by_hostname?: string | null;
  created_by_client?: string | null;
  created_by_display_name?: string | null;
  created_by_avatar_url?: string | null;
}

// Define the shape of the API response
interface ApiResponse {
  items: ApiMemoryItem[];
  total: number;
  page: number;
  size: number;
  pages: number;
}


interface RelatedMemoryItem {
  id: string;
  content: string;
  created_at: number;
  state: string;
  app_id: string;
  app_name: string;
  categories: string[];
  metadata_: Record<string, any>;
}

interface RelatedMemoriesResponse {
  items: RelatedMemoryItem[];
  total: number;
  page: number;
  size: number;
  pages: number;
}

interface UseMemoriesApiReturn {
  fetchMemories: (
    query?: string,
    page?: number,
    size?: number,
    filters?: {
      apps?: string[];
      categories?: string[];
      sortColumn?: string;
      sortDirection?: 'asc' | 'desc';
      showArchived?: boolean;
    }
  ) => Promise<{ memories: Memory[]; total: number; pages: number }>;
  fetchMemoryById: (memoryId: string) => Promise<void>;
  fetchAccessLogs: (
    memoryId: string,
    page?: number,
    pageSize?: number,
    options?: { channel?: AccessChannelFilter | null; grouped?: boolean },
  ) => Promise<void>;
  fetchRelatedMemories: (memoryId: string) => Promise<void>;
  createMemory: (text: string) => Promise<void>;
  deleteMemories: (memoryIds: string[]) => Promise<void>;
  updateMemory: (memoryId: string, content: string) => Promise<void>;
  updateMemoryState: (memoryIds: string[], state: string) => Promise<void>;
  deletionPolicy: DeletionPolicy | null;
  isLoading: boolean;
  error: string | null;
  hasUpdates: number;
  memories: Memory[];
  selectedMemory: SimpleMemory | null;
}

export const useMemoriesApi = (): UseMemoriesApiReturn => {
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [hasUpdates, setHasUpdates] = useState<number>(0);
  const [deletionPolicy, setDeletionPolicy] = useState<DeletionPolicy | null>(null);
  const dispatch = useDispatch<AppDispatch>();
  const user_id = useSelector((state: RootState) => state.profile.userId);
  const apiSessionReady = useSelector(
    (state: RootState) => state.profile.apiSessionStatus === "valid",
  );
  const memories = useSelector((state: RootState) => state.memories.memories);
  const selectedMemory = useSelector((state: RootState) => state.memories.selectedMemory);

  useEffect(() => {
    if (!apiSessionReady) return;

    let cancelled = false;
    (async () => {
      try {
        const response = await axios.get<DeletionPolicy>(
          `${getApiUrl()}/api/v1/memories/deletion-policy`,
        );
        if (!cancelled) setDeletionPolicy(response.data);
      } catch {
        if (!cancelled) {
          setDeletionPolicy({
            memory_delete_allowed: false,
            bulk_delete_allowed: false,
            message:
              "Não foi possível verificar a política de exclusão neste servidor.",
          });
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [apiSessionReady]);

  
  const fetchMemories = useCallback(async (
    query?: string,
    page: number = 1,
    size: number = 10,
    filters?: {
      apps?: string[];
      categories?: string[];
      sortColumn?: string;
      sortDirection?: 'asc' | 'desc';
      showArchived?: boolean;
    }
  ): Promise<{ memories: Memory[], total: number, pages: number }> => {
    setIsLoading(true);
    setError(null);
    try {
      const response = await axios.post<ApiResponse>(
        `${getApiUrl()}/api/v1/memories/shared-filter`,
        {
          user_id: user_id,
          page: page,
          size: size,
          search_query: query,
          app_ids: filters?.apps,
          category_ids: filters?.categories,
          sort_column: filters?.sortColumn?.toLowerCase(),
          sort_direction: filters?.sortDirection,
          show_archived: filters?.showArchived,
          source: "shared",
        }
      );

      const adaptedMemories: Memory[] = response.data.items.map((item: ApiMemoryItem) => ({
        id: item.id,
        memory: item.content,
        created_at: new Date(item.created_at).getTime(),
        state: item.state as "active" | "paused" | "archived" | "deleted",
        metadata: item.metadata_,
        categories: item.categories as Category[],
        client: 'api',
        app_name: item.app_name,
        group: item.group ?? null,
        created_by_hostname: item.created_by_hostname ?? null,
        created_by_client: item.created_by_client ?? null,
        created_by_display_name: item.created_by_display_name ?? null,
        created_by_avatar_url: item.created_by_avatar_url ?? null,
      }));
      setIsLoading(false);
      dispatch(setMemoriesSuccess(adaptedMemories));
      return {
        memories: adaptedMemories,
        total: response.data.total,
        pages: response.data.pages
      };
    } catch (err: any) {
      const errorMessage = err.response?.data?.detail || err.message || 'Falha ao buscar memórias';
      setError(errorMessage);
      setIsLoading(false);
      throw err;
    }
  }, [user_id, dispatch]);

  const createMemory = async (text: string): Promise<void> => {
    try {
      const memoryData = {
        user_id: user_id,
        text: text,
        infer: false,
        app: "openmemory",
      }
      await axios.post<ApiMemoryItem>(`${getApiUrl()}/api/v1/memories/`, memoryData);
    } catch (err: any) {
      const errorMessage = err.message || 'Falha ao criar memória';
      setError(errorMessage);
      setIsLoading(false);
      throw new Error(errorMessage);
    }
  };

  const deleteMemories = async (memory_ids: string[]) => {
    if (deletionPolicy && !deletionPolicy.memory_delete_allowed) {
      const blocked = new Error(deletionPolicy.message);
      setError(deletionPolicy.message);
      throw blocked;
    }
    if (
      memory_ids.length > 1 &&
      deletionPolicy &&
      !deletionPolicy.bulk_delete_allowed
    ) {
      const msg =
        "Exclusão em lote desabilitada. Defina MEM0_ALLOW_BULK_DELETE=1 no servidor.";
      setError(msg);
      throw new Error(msg);
    }
    try {
      await axios.post(`${getApiUrl()}/api/v1/memories/actions/delete`, {
        memory_ids,
        user_id,
      });
      dispatch(setMemoriesSuccess(memories.filter((memory: Memory) => !memory_ids.includes(memory.id))));
    } catch (err: unknown) {
      const errorMessage = parseApiError(err, "Falha ao excluir memórias");
      setError(errorMessage);
      setIsLoading(false);
      throw new Error(errorMessage);
    }
  };

  const fetchMemoryById = async (memoryId: string): Promise<void> => {
    if (memoryId === "") {
      return;
    }
    setIsLoading(true);
    setError(null);
    try {
      const response = await axios.get<SimpleMemory>(
        `${getApiUrl()}/api/v1/memories/${memoryId}?user_id=${user_id}`
      );
      setIsLoading(false);
      dispatch(setSelectedMemory({
        ...response.data,
        metadata_: response.data.metadata_ ?? undefined,
        created_by_hostname: response.data.created_by_hostname
          ?? (response.data.metadata_ as Record<string, unknown> | undefined)?.hostname as string | undefined
          ?? (response.data.metadata_ as Record<string, unknown> | undefined)?.user_id as string | undefined,
        created_by_client: response.data.created_by_client
          ?? (response.data.metadata_ as Record<string, unknown> | undefined)?.mcp_client as string | undefined,
        created_by_display_name: response.data.created_by_display_name ?? null,
        created_by_avatar_url: response.data.created_by_avatar_url ?? null,
      }));
    } catch (err: any) {
      const errorMessage = err.message || 'Falha ao buscar memória';
      setError(errorMessage);
      setIsLoading(false);
      throw new Error(errorMessage);
    }
  };

  const fetchAccessLogs = async (
    memoryId: string,
    page: number = 1,
    pageSize: number = 10,
    options: { channel?: AccessChannelFilter | null; grouped?: boolean } = {},
  ): Promise<void> => {
    if (memoryId === "") {
      return;
    }
    setIsLoading(true);
    setError(null);
    try {
      const response = await axios.get<AccessLogResponse>(
        `${getApiUrl()}/api/v1/memories/${memoryId}/access-log`,
        {
          params: {
            page,
            page_size: pageSize,
            ...(options.channel ? { channel: options.channel } : {}),
            ...(options.grouped === false ? { grouped: false } : {}),
          },
        },
      );
      setIsLoading(false);
      dispatch(setAccessLogs(response.data.logs));
      dispatch(setAccessLogMeta(toAccessLogMeta(response.data)));
    } catch (err: any) {
      const errorMessage = err.message || 'Falha ao buscar logs de acesso';
      setError(errorMessage);
      setIsLoading(false);
      throw new Error(errorMessage);
    }
  };

  const fetchRelatedMemories = async (memoryId: string): Promise<void> => {
    if (memoryId === "") {
      return;
    }
    setIsLoading(true);
    setError(null);
    try {
      const response = await axios.get<RelatedMemoriesResponse>(
        `${getApiUrl()}/api/v1/memories/${memoryId}/related?user_id=${user_id}`
      );

      const adaptedMemories: Memory[] = response.data.items.map((item: RelatedMemoryItem) => ({
        id: item.id,
        memory: item.content,
        created_at: item.created_at,
        state: item.state as "active" | "paused" | "archived" | "deleted",
        metadata: item.metadata_,
        categories: item.categories as Category[],
        client: 'api',
        app_name: item.app_name
      }));

      setIsLoading(false);
      dispatch(setRelatedMemories(adaptedMemories));
    } catch (err: any) {
      const errorMessage = err.message || 'Falha ao buscar memórias relacionadas';
      setError(errorMessage);
      setIsLoading(false);
      throw new Error(errorMessage);
    }
  };

  const updateMemory = async (memoryId: string, content: string): Promise<void> => {
    if (memoryId === "") {
      return;
    }
    setIsLoading(true);
    setError(null);
    try {
      await axios.put(`${getApiUrl()}/api/v1/memories/${memoryId}`, {
        memory_id: memoryId,
        memory_content: content,
        user_id: user_id
      });
      setIsLoading(false);
      setHasUpdates(hasUpdates + 1);
    } catch (err: any) {
      const errorMessage = err.message || 'Falha ao atualizar memória';
      setError(errorMessage);
      setIsLoading(false);
      throw new Error(errorMessage);
    }
  };

  const updateMemoryState = async (memoryIds: string[], state: string): Promise<void> => {
    if (memoryIds.length === 0) {
      return;
    }
    setIsLoading(true);
    setError(null);
    try {
      await axios.post(`${getApiUrl()}/api/v1/memories/actions/pause`, {
        memory_ids: memoryIds,
        all_for_app: true,
        state: state,
        user_id: user_id
      });
      dispatch(setMemoriesSuccess(memories.map((memory: Memory) => {
        if (memoryIds.includes(memory.id)) {
          return { ...memory, state: state as "active" | "paused" | "archived" | "deleted" };
        }
        return memory;
      })));

      // If archive, delete the memory
      if (state === "archived") {
        dispatch(setMemoriesSuccess(memories.filter((memory: Memory) => !memoryIds.includes(memory.id))));
      }

      // if selected memory, update it
      if (selectedMemory?.id && memoryIds.includes(selectedMemory.id)) {
        dispatch(setSelectedMemory({ ...selectedMemory, state: state as "active" | "paused" | "archived" | "deleted" }));
      }

      setIsLoading(false);
      setHasUpdates(hasUpdates + 1);
    } catch (err: any) {
      const errorMessage = err.message || 'Falha ao atualizar memória state';
      setError(errorMessage);
      setIsLoading(false);
      throw new Error(errorMessage);
    }
  };

  return {
    fetchMemories,
    fetchMemoryById,
    fetchAccessLogs,
    fetchRelatedMemories,
    createMemory,
    deleteMemories,
    updateMemory,
    updateMemoryState,
    deletionPolicy,
    isLoading,
    error,
    hasUpdates,
    memories,
    selectedMemory
  };
};