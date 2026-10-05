"use client";

import { useState } from "react";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

type RenameProjectDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  currentName: string;
  loading?: boolean;
  onConfirm: (newName: string) => void | Promise<void>;
};

export function RenameProjectDialog({
  open,
  onOpenChange,
  currentName,
  loading = false,
  onConfirm,
}: RenameProjectDialogProps) {
  const [typedName, setTypedName] = useState(currentName);

  const handleOpenChange = (next: boolean) => {
    if (!next) setTypedName(currentName);
    onOpenChange(next);
  };

  const canConfirm = typedName.trim().length > 0 && typedName.trim() !== currentName && !loading;

  return (
    <AlertDialog open={open} onOpenChange={handleOpenChange}>
      <AlertDialogContent className="bg-zinc-900 border-zinc-800">
        <AlertDialogHeader>
          <AlertDialogTitle>Renomear ou Mesclar Projeto</AlertDialogTitle>
          <AlertDialogDescription asChild>
            <div className="space-y-3 text-sm text-zinc-400">
              <p>
                Digite o novo nome para o projeto <strong className="text-zinc-200">{currentName}</strong>.
              </p>
              <p>
                Se o novo nome já existir, nada é movido agora: é criada uma
                <strong className="text-emerald-400"> proposta de unificação</strong> que precisa ser aprovada em Governança.
              </p>
            </div>
          </AlertDialogDescription>
        </AlertDialogHeader>
        <div className="py-2">
          <Label htmlFor="rename-project-name" className="sr-only">
            Novo nome do projeto
          </Label>
          <Input
            id="rename-project-name"
            value={typedName}
            onChange={(e) => setTypedName(e.target.value)}
            placeholder={currentName}
            className="bg-zinc-950 border-zinc-700"
            autoComplete="off"
          />
        </div>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={loading}>Cancelar</AlertDialogCancel>
          <AlertDialogAction
            disabled={!canConfirm}
            onClick={(e) => {
              e.preventDefault();
              void onConfirm(typedName.trim());
            }}
            className="bg-emerald-600 hover:bg-emerald-700 disabled:opacity-50"
          >
            {loading ? "Processando…" : "Renomear"}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
