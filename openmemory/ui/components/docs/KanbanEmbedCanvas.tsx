"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import axios from "axios";
import { Button } from "@/components/ui/button";
import { getApiUrl } from "@/lib/api-url";
import {
  formatKanbanLoadError,
  isUnmappedBoardError,
} from "@/lib/kanbanEmbed";
import { useApiSessionReady } from "@/hooks/useApiSessionReady";

export type KanbanEmbedInfo = {
  embed_url: string;
  access_token: string;
  board_id?: string;
};

type Props = {
  /** PLANKA board snowflake from Next route (deep-link / refresh). */
  boardId?: string;
  reloadToken?: number;
};

const BOARD_ID_RE = /^[0-9]{1,32}$/;
const LAST_BOARD_KEY = "mem0_kanban_last_board";

function shellPathForBoard(boardId: string | null | undefined): string {
  if (boardId && BOARD_ID_RE.test(boardId)) {
    return `/docs/boards/${boardId}`;
  }
  return "/docs";
}

function boardIdFromPathname(pathname: string): string | undefined {
  const m = pathname.match(/^\/docs\/boards\/([0-9]{1,32})\/?$/);
  return m?.[1];
}

function readLastBoard(): string | undefined {
  try {
    const raw = sessionStorage.getItem(LAST_BOARD_KEY);
    return raw && BOARD_ID_RE.test(raw) ? raw : undefined;
  } catch {
    return undefined;
  }
}

function writeLastBoard(boardId: string | undefined) {
  try {
    if (boardId && BOARD_ID_RE.test(boardId)) {
      sessionStorage.setItem(LAST_BOARD_KEY, boardId);
    } else {
      sessionStorage.removeItem(LAST_BOARD_KEY);
    }
  } catch {
    // private mode
  }
}

function resolveBootBoardId(propBoardId?: string): string | undefined {
  if (propBoardId && BOARD_ID_RE.test(propBoardId)) return propBoardId;
  if (typeof window === "undefined") return undefined;
  return boardIdFromPathname(window.location.pathname) || readLastBoard();
}

function buildIframeSrc(embedUrl: string, accessToken: string): string {
  const url = new URL(embedUrl, window.location.origin);
  url.searchParams.set("mem0_token", accessToken);
  url.searchParams.set("embed", "1");
  return url.toString();
}

function syncShellPath(boardId: string | undefined) {
  if (typeof window === "undefined") return;
  const target = shellPathForBoard(boardId);
  if (window.location.pathname !== target) {
    window.history.replaceState(window.history.state, "", target);
  }
}

/**
 * Canvas Kanban same-origin.
 * Depois do primeiro load, o iframe NÃO é tocado ao focar a aba / revalidar
 * sessão (token já está no cookie do PLANKA). Soft-nav só atualiza URL.
 */
export function KanbanEmbedCanvas({ boardId: propBoardId, reloadToken = 0 }: Props) {
  const apiSessionReady = useApiSessionReady();
  const [bootBoardId] = useState(() => resolveBootBoardId(propBoardId));
  const [activeBoardId, setActiveBoardId] = useState<string | undefined>(bootBoardId);
  const [iframeSrc, setIframeSrc] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  /** Quadro pelo qual o iframe foi montado (só muda em hard load). */
  const [mountBoardId, setMountBoardId] = useState<string | undefined>(bootBoardId);
  const iframeSrcRef = useRef<string | null>(null);
  const mountBoardRef = useRef<string | undefined>(bootBoardId);
  const didInitialLoad = useRef(false);

  useEffect(() => {
    iframeSrcRef.current = iframeSrc;
  }, [iframeSrc]);

  useEffect(() => {
    mountBoardRef.current = mountBoardId;
  }, [mountBoardId]);

  const fetchEmbed = useCallback(async (boardId: string | undefined) => {
    if (boardId) {
      const res = await axios.get<KanbanEmbedInfo>(
        `${getApiUrl()}/api/v1/specs/kanban-boards/${boardId}`,
      );
      return res.data;
    }
    const res = await axios.get<KanbanEmbedInfo>(
      `${getApiUrl()}/api/v1/specs/kanban-home`,
    );
    return res.data;
  }, []);

  const hardLoad = useCallback(
    async (
      boardId: string | undefined,
      options?: { allowStaleBoardFallback?: boolean },
    ) => {
      if (!apiSessionReady) {
        setLoading(true);
        return;
      }
      if (boardId && !BOARD_ID_RE.test(boardId)) {
        setLoading(false);
        setError("ID de quadro inválido");
        setIframeSrc(null);
        iframeSrcRef.current = null;
        return;
      }
      setLoading(true);
      setError(null);
      try {
        const data = await fetchEmbed(boardId);
        const nextSrc = buildIframeSrc(data.embed_url, data.access_token);
        setIframeSrc(nextSrc);
        iframeSrcRef.current = nextSrc;
        setMountBoardId(boardId);
        mountBoardRef.current = boardId;
        setActiveBoardId(boardId);
        writeLastBoard(boardId);
        syncShellPath(boardId);
        didInitialLoad.current = true;
      } catch (err: unknown) {
        const canFallbackStaleBoard =
          Boolean(boardId) &&
          options?.allowStaleBoardFallback !== false &&
          isUnmappedBoardError(err) &&
          !propBoardId;
        if (canFallbackStaleBoard) {
          writeLastBoard(undefined);
          syncShellPath(undefined);
          setActiveBoardId(undefined);
          setMountBoardId(undefined);
          mountBoardRef.current = undefined;
          await hardLoad(undefined, { allowStaleBoardFallback: false });
          return;
        }
        setIframeSrc(null);
        iframeSrcRef.current = null;
        setError(formatKanbanLoadError(err));
      } finally {
        setLoading(false);
      }
    },
    [apiSessionReady, fetchEmbed, propBoardId],
  );

  // Carga inicial (uma vez) quando a sessão fica válida.
  useEffect(() => {
    if (!apiSessionReady) return;
    if (didInitialLoad.current && iframeSrcRef.current) return;
    void hardLoad(mountBoardRef.current ?? activeBoardId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiSessionReady]);

  // reloadToken explícito (criar task etc.) — remount com JWT novo.
  const prevReload = useRef(reloadToken);
  useEffect(() => {
    if (prevReload.current === reloadToken) return;
    prevReload.current = reloadToken;
    if (!apiSessionReady) return;
    void hardLoad(mountBoardRef.current ?? activeBoardId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reloadToken, apiSessionReady]);

  // O botão Atualizar do shell precisa renovar o JWT do iframe. Isso também
  // recupera o canvas depois que o sidecar PLANKA reinicia e perde o socket.
  useEffect(() => {
    const onReload = () => {
      if (!apiSessionReady) return;
      void hardLoad(mountBoardRef.current ?? activeBoardId);
    };
    window.addEventListener("mem0-kanban-reload", onReload);
    return () => window.removeEventListener("mem0-kanban-reload", onReload);
  }, [activeBoardId, apiSessionReady, hardLoad]);

  // Deep-link Next: prop mudou → hard load só se for outro quadro.
  useEffect(() => {
    if (!propBoardId || !BOARD_ID_RE.test(propBoardId)) return;
    if (propBoardId === mountBoardRef.current && iframeSrcRef.current) return;
    void hardLoad(propBoardId);
  }, [propBoardId, hardLoad]);

  // Soft-nav do iframe: URL + storage apenas (nunca mexe no src). Quando o
  // JWT expira, o iframe pede um token novo ao shell em vez de mostrar login.
  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      if (event.origin !== window.location.origin) return;
      const data = event.data;
      if (!data || data.source !== "mem0-kanban") return;
      if (data.type === "auth-expired") {
        if (!apiSessionReady) return;
        void hardLoad(mountBoardRef.current ?? activeBoardId);
        return;
      }
      if (data.type !== "path") return;
      const nextBoard =
        typeof data.boardId === "string" && BOARD_ID_RE.test(data.boardId)
          ? data.boardId
          : undefined;
      writeLastBoard(nextBoard);
      setActiveBoardId(nextBoard);
      // Soft-nav: o iframe já está no quadro; alinhar mount ref sem remount.
      mountBoardRef.current = nextBoard;
      setMountBoardId(nextBoard);
      syncShellPath(nextBoard);
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [activeBoardId, apiSessionReady, hardLoad]);

  // Foco/aba: só corrige a URL do shell. Nunca recarrega o iframe.
  useEffect(() => {
    const onFocus = () => {
      if (document.visibilityState === "hidden") return;
      const wanted =
        boardIdFromPathname(window.location.pathname) ||
        readLastBoard() ||
        mountBoardRef.current;
      syncShellPath(wanted);
      if (wanted) {
        setActiveBoardId(wanted);
        writeLastBoard(wanted);
      }
    };
    document.addEventListener("visibilitychange", onFocus);
    window.addEventListener("focus", onFocus);
    return () => {
      document.removeEventListener("visibilitychange", onFocus);
      window.removeEventListener("focus", onFocus);
    };
  }, []);

  const showLoading = (!apiSessionReady || loading) && !iframeSrc;

  if (showLoading) {
    return (
      <div
        className="flex min-h-0 flex-1 items-center justify-center bg-card/40 text-sm text-muted-foreground"
        data-testid="kanban-home-loading"
      >
        Carregando Kanban…
      </div>
    );
  }

  if (error && !iframeSrc) {
    return (
      <div
        className="flex min-h-0 flex-1 flex-col items-center justify-center gap-3 bg-card/40 p-6 text-center"
        data-testid="kanban-home-error"
      >
        <p className="text-sm text-destructive" role="alert">
          {error}
        </p>
        <div className="flex flex-wrap items-center justify-center gap-2">
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => void hardLoad(activeBoardId)}
          >
            Tentar de novo
          </Button>
          {propBoardId ? (
            <Button
              type="button"
              size="sm"
              variant="secondary"
              onClick={() => void hardLoad(undefined)}
            >
              Ir para home do Kanban
            </Button>
          ) : null}
        </div>
      </div>
    );
  }

  if (!iframeSrc) return null;

  // key estável após o primeiro load — soft-nav / foco não remonta.
  return (
    <iframe
      title="Kanban"
      src={iframeSrc}
      className="min-h-0 w-full flex-1 border-0 bg-background"
      data-testid={activeBoardId ? "kanban-board-canvas" : "kanban-home-canvas"}
      allow="clipboard-read; clipboard-write"
    />
  );
}

export default KanbanEmbedCanvas;
