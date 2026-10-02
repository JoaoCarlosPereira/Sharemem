"use client";

import { useSelector } from "react-redux";

import type { ApiSessionStatus } from "@/store/profileSlice";
import type { RootState } from "@/store/store";

export function useApiSessionStatus(): ApiSessionStatus {
  return useSelector((state: RootState) => state.profile.apiSessionStatus);
}

/** True após GET /auth/me confirmar o Bearer da API. */
export function useApiSessionReady(): boolean {
  return useApiSessionStatus() === "valid";
}

/**
 * True quando o AuthBridge terminou de decidir a sessão (`valid` ou `invalid`).
 *
 * Leituras auditadas (GET de memória) devem esperar este sinal: antes dele o
 * Bearer ainda não está no axios e o servidor grava o leitor como anônimo.
 */
export function useApiSessionSettled(): boolean {
  const status = useApiSessionStatus();
  return status === "valid" || status === "invalid";
}
