"use client";

import "@/styles/animation.css";
import { useEffect, useRef } from "react";
import { useApiSessionSettled } from "@/hooks/useApiSessionReady";
import { useMemoriesApi } from "@/hooks/useMemoriesApi";
import { use } from "react";
import { MemorySkeleton } from "@/skeleton/MemorySkeleton";
import { MemoryDetails } from "./components/MemoryDetails";
import UpdateMemory from "@/components/shared/update-memory";
import { useUI } from "@/hooks/useUI";
import { RootState } from "@/store/store";
import { useSelector } from "react-redux";
import NotFound from "@/app/not-found";

function MemoryContent({ id }: { id: string }) {
  const { fetchMemoryById, isLoading, error } = useMemoriesApi();
  const memory = useSelector(
    (state: RootState) => state.memories.selectedMemory
  );

  const sessionSettled = useApiSessionSettled();
  // Uma única leitura (= uma linha de auditoria) por memória aberta, e só depois
  // que a sessão estiver decidida — senão o Bearer ainda não foi anexado e o
  // usuário logado seria gravado como "Interface Web (sem login)".
  const fetchedId = useRef<string | null>(null);

  useEffect(() => {
    if (!sessionSettled || fetchedId.current === id) return;
    fetchedId.current = id;
    fetchMemoryById(id).catch((err) => {
      console.error("Failed to load memory:", err);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, sessionSettled]);

  if (!sessionSettled || fetchedId.current !== id || isLoading) {
    return <MemorySkeleton />;
  }

  if (error) {
    return <NotFound message={error} />;
  }

  if (!memory || memory.id !== id) {
    return <NotFound message="Memória não encontrada" statusCode={404} />;
  }

  return <MemoryDetails memory_id={memory.id} />;
}

export default function MemoryPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const resolvedParams = use(params);
  const { updateMemoryDialog, handleCloseUpdateMemoryDialog } = useUI();
  return (
    <div>
      <div className="animate-fade-slide-down delay-1">
        <UpdateMemory
          memoryId={updateMemoryDialog.memoryId || ""}
          memoryContent={updateMemoryDialog.memoryContent || ""}
          open={updateMemoryDialog.isOpen}
          onOpenChange={handleCloseUpdateMemoryDialog}
        />
      </div>
      <div className="animate-fade-slide-down delay-2">
        <MemoryContent id={resolvedParams.id} />
      </div>
    </div>
  );
}
