/**
 * Segredos mascarados devolvidos por ``/api/v1/config`` (ver
 * ``openmemory/api/app/utils/secret_mask.py``): ``****`` + últimos 4 chars.
 *
 * Reenviar a máscara inalterada é seguro — a API mantém o valor real. Mas se o
 * operador começar a digitar sobre a máscara, o valor resultante ainda começa
 * com ``****`` e seria tratado como "inalterado". ``nextSecretInput`` evita
 * isso: digitar sobre uma máscara substitui o campo pelo texto novo.
 *
 * Apagar a máscara (backspace) deixa o campo vazio; ``""`` num campo que veio
 * mascarado significa **manter** o segredo atual (``preserveMaskedSecrets``
 * recoloca a máscara antes do envio). Para remover de fato o segredo, use a
 * ação explícita ``CLEARED_SECRET`` (``null``): a API descarta a chave.
 */
export const SECRET_MASK_PREFIX = "****";

/** Valor usado pela ação explícita "remover chave" (a API descarta ``null``). */
export const CLEARED_SECRET = null;

export function isMaskedSecret(value: unknown): value is string {
  return typeof value === "string" && value.startsWith(SECRET_MASK_PREFIX);
}

export function nextSecretInput(current: unknown, typed: string): string {
  if (!isMaskedSecret(current)) return typed;
  if (typed === current) return typed;
  // Acrescentou texto ao final da máscara → fica só o que foi digitado.
  if (typed.startsWith(current)) return typed.slice(current.length);
  // Apagou parte da máscara → limpa o campo para digitar a chave nova
  // (vazio = manter o segredo atual, ver preserveMaskedSecrets).
  if (isMaskedSecret(typed) || current.startsWith(typed)) return "";
  // Seleção total + digitação/colagem → valor novo.
  return typed;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Cópia de ``next`` em que todo campo ``""`` cujo correspondente em ``loaded``
 * (config recebida da API) estava mascarado volta a ser a máscara — a API então
 * mantém o segredo persistido em vez de gravar ``""`` por cima dele.
 */
export function preserveMaskedSecrets<T>(next: T, loaded: unknown): T {
  if (next === "" && isMaskedSecret(loaded)) return loaded as T;
  if (Array.isArray(next)) {
    const loadedArr = Array.isArray(loaded) ? loaded : [];
    return next.map((item, i) => preserveMaskedSecrets(item, loadedArr[i])) as T;
  }
  if (isPlainObject(next)) {
    const loadedObj = isPlainObject(loaded) ? loaded : {};
    const out: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(next)) {
      out[key] = preserveMaskedSecrets(value, loadedObj[key]);
    }
    return out as T;
  }
  return next;
}
