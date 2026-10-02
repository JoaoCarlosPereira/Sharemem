#!/usr/bin/env bash
# Rebuild e reinicia API + UI (e workers que compartilham a mesma imagem).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

run_docker() {
  if docker "$@" 2>/dev/null; then
    return 0
  fi
  if sudo -n docker "$@" 2>/dev/null; then
    return 0
  fi
  echo ">>> Executando com sudo (pode pedir senha)..." >&2
  sudo docker "$@"
}

COMPOSE_FILE="docker-compose.scale.yml"
if docker compose version >/dev/null 2>&1; then
  COMPOSE() { docker compose -f "$COMPOSE_FILE" "$@"; }
elif [ -x "$HOME/.docker/cli-plugins/docker-compose" ]; then
  COMPOSE() { "$HOME/.docker/cli-plugins/docker-compose" -f "$COMPOSE_FILE" "$@"; }
else
  echo "ERRO: docker compose v2 não encontrado." >&2
  exit 1
fi

compose() {
  if COMPOSE "$@" 2>/dev/null; then
    return 0
  fi
  if sudo -n env HOME="$HOME" PATH="$PATH" docker compose -f "$COMPOSE_FILE" "$@" 2>/dev/null; then
    return 0
  fi
  echo ">>> compose com sudo (pode pedir senha)..." >&2
  sudo env HOME="$HOME" PATH="$PATH" docker compose -f "$COMPOSE_FILE" "$@"
}

echo "==> Build openmemory-mcp (API)..."
run_docker build -f api/Dockerfile -t mem0/openmemory-mcp ..

echo "==> Build openmemory-ui..."
# .env.example embeds NEXT_PUBLIC_* placeholders so entrypoint.sh can inject
# GOOGLE_CLIENT_ID / AUTH_UI_REQUIRED at runtime (compose already passes them).
# Building without those placeholders made the client bundle think Google auth
# was off ("modo legado LAN") even though NextAuth still had the provider.
run_docker build -f ui/Dockerfile -t mem0/openmemory-ui:latest ui/

echo "==> Recriando containers (API, workers, UI)..."
compose up -d --no-deps --force-recreate \
  openmemory-mcp openmemory-write-worker openmemory-governance-worker openmemory-backup-worker openmemory-ui

API="http://127.0.0.1:8765"
# Timeouts em toda chamada curl: API travada não pode pendurar o script.
CURL_TIMEOUTS=(--connect-timeout 3 --max-time 5)
READY_TIMEOUT_S="${READY_TIMEOUT_S:-90}"

# Prontidão: qualquer resposta HTTP (inclusive 401/403) significa "API no ar".
# 000 = sem resposta (conexão recusada/timeout) → continua aguardando.
anon_config_code() {
  curl -s "${CURL_TIMEOUTS[@]}" -o /dev/null -w '%{http_code}' "${API}/api/v1/config" 2>/dev/null || true
}

echo "==> Aguardando API (até ${READY_TIMEOUT_S}s)..."
anon_code="000"
deadline=$(( SECONDS + READY_TIMEOUT_S ))
while [ "$SECONDS" -lt "$deadline" ]; do
  anon_code="$(anon_config_code)"
  anon_code="${anon_code:-000}"
  if [ "$anon_code" != "000" ]; then
    break
  fi
  sleep 2
done

echo "==> Smoke GET /api/v1/config (somente leitura)..."
# Não destrutivo: NÃO grava nada na config de produção. Verifica que a rota
# exige admin, responde 200 com o token e devolve a api_key mascarada.
# /api/v1/config exige admin (require_admin): usa ADMIN_TOKEN do ambiente ou do .env.
ADMIN_TOKEN="${ADMIN_TOKEN:-$(grep -E '^ADMIN_TOKEN=' .env 2>/dev/null | tail -n1 | cut -d= -f2- | sed -e 's/^["'"'"']//' -e 's/["'"'"']$//' || true)}"

case "$anon_code" in
  401|403) ;;
  000)
    echo "ERRO: API não respondeu em ${API} após ${READY_TIMEOUT_S}s (HTTP 000). Logs: sudo docker logs openmemory_api --tail 40" >&2
    exit 1
    ;;
  2??)
    echo "ERRO DE SEGURANÇA: GET /api/v1/config SEM credencial retornou HTTP ${anon_code} — a config (api_key do LLM) está exposta a anônimos. Verifique require_admin no router de config." >&2
    exit 1
    ;;
  *)
    echo "ERRO: GET /api/v1/config sem credencial retornou HTTP ${anon_code} (esperado 401/403). Logs: sudo docker logs openmemory_api --tail 40" >&2
    exit 1
    ;;
esac

if [ -z "$ADMIN_TOKEN" ]; then
  echo "AVISO: ADMIN_TOKEN ausente (ambiente/.env) — smoke autenticado pulado; anônimo => ${anon_code} OK." >&2
else
  # Header via arquivo 600 (curl -H @arquivo): o token não aparece no ps.
  HDR="$(mktemp)"
  BODY="$(mktemp)"
  chmod 600 "$HDR"
  trap 'rm -f "$HDR" "$BODY"' EXIT
  printf 'X-Admin-Token: %s\n' "$ADMIN_TOKEN" > "$HDR"
  code="$(curl -s "${CURL_TIMEOUTS[@]}" -o "$BODY" -w '%{http_code}' -H @"$HDR" "${API}/api/v1/config" || true)"
  if [ "$code" != "200" ]; then
    echo "ERRO: GET /api/v1/config autenticado retornou HTTP ${code}. Logs: sudo docker logs openmemory_api --tail 40" >&2
    exit 1
  fi
  # api_key do LLM deve vir mascarada (****…), como env:VAR, ausente ou vazia.
  if ! python3 - "$BODY" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1]))
llm = ((cfg.get("mem0") or {}).get("llm") or {}).get("config") or {}
key = llm.get("api_key")
ok = key in (None, "") or key.startswith("****") or key.startswith("env:")
sys.exit(0 if ok else 1)
PY
  then
    echo "ERRO: GET /api/v1/config devolveu api_key sem máscara." >&2
    exit 1
  fi
  echo "OK: config exige admin (anônimo ${anon_code}), 200 autenticado e api_key mascarada."
fi

IP="$(hostname -I | awk '{print $1}')"
echo ""
echo "Pronto."
echo "  UI:  http://${IP}:3000"
echo "  API: http://${IP}:8765"
echo "  Use Ctrl+Shift+R no navegador."
