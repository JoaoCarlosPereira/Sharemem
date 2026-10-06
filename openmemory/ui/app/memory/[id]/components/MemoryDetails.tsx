"use client";
import { MemoryActions } from "./MemoryActions";
import { ArrowLeft, Copy, Check } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useRouter } from "next/navigation";
import { AccessLog } from "./AccessLog";
import Categories from "@/components/shared/categories";
import { useState } from "react";
import { useSelector } from "react-redux";
import { RootState } from "@/store/store";
import { RelatedMemories } from "./RelatedMemories";
import { AttributionBadge } from "@/components/shared/attribution-badge";

interface MemoryDetailsProps {
  memory_id: string;
}

/** Exibe a memória já carregada por `page.tsx` (não busca de novo: cada GET grava auditoria). */
export function MemoryDetails({ memory_id }: MemoryDetailsProps) {
  const router = useRouter();
  const memory = useSelector(
    (state: RootState) => state.memories.selectedMemory
  );
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    if (memory?.id) {
      await navigator.clipboard.writeText(memory.id);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  };

  return (
    <div className="container mx-auto py-6 px-4">
      <Button
        variant="ghost"
        className="mb-4 text-zinc-400 hover:text-white"
        onClick={() => router.back()}
      >
        <ArrowLeft className="h-4 w-4 mr-2" />
        Voltar para Memórias
      </Button>
      <div className="flex gap-4 w-full">
        <div className="rounded-lg w-2/3 border h-fit pb-2 border-zinc-800 bg-zinc-900 overflow-hidden">
          <div className="">
            <div className="flex px-6 py-3 justify-between items-center mb-6 bg-zinc-800 border-b border-zinc-800">
              <div className="flex items-center gap-2">
                <h1 className="font-semibold text-white">
                  Memória{" "}
                  <span className="ml-1 text-zinc-400 text-sm font-normal">
                    #{memory?.id?.slice(0, 6)}
                  </span>
                </h1>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-4 w-4 text-zinc-400 hover:text-white -ml-[5px] mt-1"
                  onClick={handleCopy}
                >
                  {copied ? (
                    <Check className="h-3 w-3" />
                  ) : (
                    <Copy className="h-3 w-3" />
                  )}
                </Button>
              </div>
              <MemoryActions
                memoryId={memory?.id || ""}
                memoryContent={memory?.text || ""}
                memoryState={memory?.state || ""}
              />
            </div>

            <div className="px-6 py-2">
              <div className="border-l-2 border-primary pl-4 mb-6">
                <p
                  className={`${
                    memory?.state === "archived" || memory?.state === "paused"
                      ? "text-zinc-400"
                      : "text-white"
                  }`}
                >
                  {memory?.text}
                </p>
              </div>

              <div className="mt-6 pt-4 border-t border-zinc-800">
                <div className="flex justify-between items-center">
                  <div className="">
                    <Categories
                      categories={memory?.categories || []}
                      isPaused={
                        memory?.state === "archived" ||
                        memory?.state === "paused"
                      }
                    />
                  </div>
                  <div className="flex items-center gap-2 min-w-[300px] justify-end">
                    <AttributionBadge
                      appName={memory?.app_name}
                      clientName={memory?.created_by_client}
                      hostname={memory?.created_by_hostname}
                      displayName={memory?.created_by_display_name}
                      avatarUrl={memory?.created_by_avatar_url}
                      metadata={memory?.metadata_}
                      iconSize={16}
                    />
                  </div>
                </div>

                {/* <div className="flex justify-end gap-2 w-full mt-2">
                <p className="text-sm font-semibold text-primary my-auto">
                    {new Date(memory.created_at).toLocaleString()}
                  </p>
                </div> */}
              </div>
            </div>
          </div>
        </div>
        <div className="w-1/3 flex flex-col gap-4">
          <AccessLog memoryId={memory?.id || ""} />
          <RelatedMemories memoryId={memory?.id || ""} />
        </div>
      </div>
    </div>
  );
}
