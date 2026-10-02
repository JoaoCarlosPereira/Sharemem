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
# INSTALL_RERANK/RERANK_PRELOAD_MODEL: rerank local opcional (docs/runbooks/rerank.md).
# Repassados para que um rebuild nao remova silenciosamente o torch de uma imagem
# em que o operador ligou o rerank. Default 0 = imagem sem torch.
run_docker build -f api/Dockerfile \
  --build-arg INSTALL_RERANK="${INSTALL_RERANK:-0}" \
  --build-arg RERANK_PRELOAD_MODEL="${RERANK_PRELOAD_MODEL:-}" \
  -t mem0/openmemory-mcp ..

echo "==> Build openmemory-ui..."
# .env.example embeds NEXT_PUBLIC_* placeholders so entrypoint.sh can inject
# GOOGLE_CLIENT_ID / AUTH_UI_REQUIRED at runtime (compose already passes them).
# Building without those placeholders made the client bundle think Google auth
# was off ("modo legado LAN") even though NextAuth still had the provider.
run_docker build -f ui/Dockerfile -t mem0/openmemory-ui:latest ui/

echo "==> Recriando containers (API, workers, UI)..."
compose up -d --no-deps --force-recreate \
  openmemory-mcp openmemory-write-worker openmemory-governance-worker openmemory-backup-worker openmemory-ui

echo "==> Aguardando API..."
sleep 5

echo "==> Teste PUT /api/v1/config..."
if curl -sf -X PUT "http://127.0.0.1:8765/api/v1/config" \
  -H "Content-Type: application/json" \
  -d '{"openmemory":{"custom_instructions":null},"mem0":{"llm":{"provider":"openai","config":{"model":"rebuild-ok","temperature":0.1,"max_tokens":2000,"api_key":"env:OPENAI_API_KEY"}},"embedder":{"provider":"ollama","config":{"model":"nomic-embed-text:latest","ollama_base_url":"http://host.docker.internal:11434"}}}}' \
  | grep -q rebuild-ok; then
  echo "OK: configuração salva com sucesso."
else
  echo "AVISO: teste PUT falhou. Logs: sudo docker logs openmemory_api --tail 40" >&2
  exit 1
fi

IP="$(hostname -I | awk '{print $1}')"
echo ""
echo "Pronto."
echo "  UI:  http://${IP}:3000"
echo "  API: http://${IP}:8765"
echo "  Use Ctrl+Shift+R no navegador."
