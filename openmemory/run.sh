#!/bin/bash

set -e

echo "🚀 Starting OpenMemory installation..."

# Set environment variables
OPENAI_API_KEY="${OPENAI_API_KEY:-}"
USER="${USER:-$(whoami)}"
NEXT_PUBLIC_API_URL="${NEXT_PUBLIC_API_URL:-http://localhost:8765}"

if [ -z "$OPENAI_API_KEY" ]; then
  echo "❌ OPENAI_API_KEY not set. Please run with: curl -sL https://raw.githubusercontent.com/mem0ai/mem0/main/openmemory/run.sh | OPENAI_API_KEY=your_api_key bash"
  echo "❌ OPENAI_API_KEY not set. You can also set it as global environment variable: export OPENAI_API_KEY=your_api_key"
  exit 1
fi

# Check if Docker is installed
if ! command -v docker &> /dev/null; then
  echo "❌ Docker not found. Please install Docker first."
  exit 1
fi

# Check if docker compose is available
if ! docker compose version &> /dev/null; then
  echo "❌ Docker Compose not found. Please install Docker Compose V2."
  exit 1
fi

# Check if the container "mem0_ui" already exists and remove it if necessary
if [ $(docker ps -aq -f name=mem0_ui) ]; then
  echo "⚠️ Found existing container 'mem0_ui'. Removing it..."
  docker rm -f mem0_ui
fi

# Find an available port starting from 3000
echo "🔍 Looking for available port for frontend..."
for port in {3000..3010}; do
  if ! lsof -i:$port >/dev/null 2>&1; then
    FRONTEND_PORT=$port
    break
  fi
done

if [ -z "$FRONTEND_PORT" ]; then
  echo "❌ Could not find an available port between 3000 and 3010"
  exit 1
fi

# ADMIN_TOKEN: /api/v1/config exige admin (require_admin). Sem ele a API
# responde 401 a tudo de config (seed do vector store e tela Configurações).
# Se ausente, gera um forte e grava em .openmemory-admin-token (chmod 600).
ADMIN_TOKEN="${ADMIN_TOKEN:-}"
ADMIN_TOKEN_FILE="${ADMIN_TOKEN_FILE:-$(pwd)/.openmemory-admin-token}"
if [ -z "$ADMIN_TOKEN" ] && [ -s "$ADMIN_TOKEN_FILE" ]; then
  ADMIN_TOKEN="$(tr -d '[:space:]' < "$ADMIN_TOKEN_FILE")"
  echo "🔑 Using ADMIN_TOKEN from ${ADMIN_TOKEN_FILE}"
fi
if [ -z "$ADMIN_TOKEN" ]; then
  if ! command -v openssl >/dev/null 2>&1; then
    echo "❌ ADMIN_TOKEN not set and openssl not found to generate one. Run with ADMIN_TOKEN=<token> bash run.sh"
    exit 1
  fi
  ADMIN_TOKEN="$(openssl rand -hex 32)"
  ( umask 077 && printf '%s\n' "$ADMIN_TOKEN" > "$ADMIN_TOKEN_FILE" )
  chmod 600 "$ADMIN_TOKEN_FILE"
  echo "🔑 ADMIN_TOKEN not set: generated a new one and saved it to ${ADMIN_TOKEN_FILE} (mode 600)."
  echo "   Use it as the X-Admin-Token header for /admin/* and /api/v1/config."
fi

# Export required variables for Compose and frontend
export ADMIN_TOKEN
export OPENAI_API_KEY
export USER
export NEXT_PUBLIC_API_URL
export NEXT_PUBLIC_USER_ID="$USER"
export FRONTEND_PORT

# Parse vector store selection (env var or flag). Default: qdrant
VECTOR_STORE="${VECTOR_STORE:-qdrant}"
EMBEDDING_DIMS="${EMBEDDING_DIMS:-1536}"

for arg in "$@"; do
  case $arg in
    --vector-store=*)
      VECTOR_STORE="${arg#*=}"
      shift
      ;;
    --vector-store)
      VECTOR_STORE="$2"
      shift 2
      ;;
    *)
      ;;
  esac
done

export VECTOR_STORE
echo "🧰 Using vector store: $VECTOR_STORE"

# Function to create compose file by merging vector store config with openmemory-mcp service
create_compose_file() {
  local vector_store=$1
  local compose_file="compose/${vector_store}.yml"
  local volume_name="${vector_store}_data"  # Vector-store-specific volume name
  
  # Check if the compose file exists
  if [ ! -f "$compose_file" ]; then
    echo "❌ Compose file not found: $compose_file"
    echo "Available vector stores: $(ls compose/*.yml | sed 's/compose\///g' | sed 's/\.yml//g' | tr '\n' ' ')"
    exit 1
  fi
  
  echo "📝 Creating docker-compose.yml using $compose_file..."
  echo "💾 Using volume: $volume_name"
  
  # Start the compose file with services section
  echo "services:" > docker-compose.yml
  
  # Extract services from the compose file and replace volume name
  # First get everything except the last volumes section
  tail -n +2 "$compose_file" | sed '/^volumes:/,$d' | sed "s/mem0_storage/${volume_name}/g" >> docker-compose.yml
  
  # Add a newline to ensure proper YAML formatting
  echo "" >> docker-compose.yml
  
  # Add the openmemory-mcp service
  cat >> docker-compose.yml <<EOF
  openmemory-mcp:
    image: mem0/openmemory-mcp:latest
    environment:
      - OPENAI_API_KEY=${OPENAI_API_KEY}
      - USER=${USER}
      - ADMIN_TOKEN=\${ADMIN_TOKEN}
EOF

  # Add vector store specific environment variables
  case "$vector_store" in
    weaviate)
      cat >> docker-compose.yml <<EOF
      - WEAVIATE_HOST=mem0_store
      - WEAVIATE_PORT=8080
EOF
      ;;
    redis)
      cat >> docker-compose.yml <<EOF
      - REDIS_URL=redis://mem0_store:6379
EOF
      ;;
    pgvector)
      cat >> docker-compose.yml <<EOF
      - PG_HOST=mem0_store
      - PG_PORT=5432
      - PG_DB=mem0
      - PG_USER=mem0
      - PG_PASSWORD=mem0
EOF
      ;;
    qdrant)
      cat >> docker-compose.yml <<EOF
      - QDRANT_HOST=mem0_store
      - QDRANT_PORT=6333
EOF
      ;;
    chroma)
      cat >> docker-compose.yml <<EOF
      - CHROMA_HOST=mem0_store
      - CHROMA_PORT=8000
EOF
      ;;
    milvus)
      cat >> docker-compose.yml <<EOF
      - MILVUS_HOST=mem0_store
      - MILVUS_PORT=19530
EOF
      ;;
    elasticsearch)
      cat >> docker-compose.yml <<EOF
      - ELASTICSEARCH_HOST=mem0_store
      - ELASTICSEARCH_PORT=9200
      - ELASTICSEARCH_USER=elastic
      - ELASTICSEARCH_PASSWORD=changeme
EOF
      ;;
    faiss)
      cat >> docker-compose.yml <<EOF
      - FAISS_PATH=/tmp/faiss
EOF
      ;;
    *)
      echo "⚠️ Unknown vector store: $vector_store. Using default Qdrant configuration."
      cat >> docker-compose.yml <<EOF
      - QDRANT_HOST=mem0_store
      - QDRANT_PORT=6333
EOF
      ;;
  esac

  # Add common openmemory-mcp service configuration
  if [ "$vector_store" = "faiss" ]; then
    # FAISS doesn't need a separate service, just volume mounts
    cat >> docker-compose.yml <<EOF
    ports:
      - "8765:8765"
    volumes:
      - openmemory_db:/usr/src/openmemory
      - ${volume_name}:/tmp/faiss

volumes:
  ${volume_name}:
  openmemory_db:
EOF
  else
    cat >> docker-compose.yml <<EOF
    depends_on:
      - mem0_store
    ports:
      - "8765:8765"
    volumes:
      - openmemory_db:/usr/src/openmemory

volumes:
  ${volume_name}:
  openmemory_db:
EOF
  fi
}

# Create docker-compose.yml file based on selected vector store
echo "📝 Creating docker-compose.yml..."
create_compose_file "$VECTOR_STORE"

# Ensure local data directories exist for bind-mounted vector stores
if [ "$VECTOR_STORE" = "milvus" ]; then
  echo "🗂️ Ensuring local data directories for Milvus exist..."
  mkdir -p ./data/milvus/etcd ./data/milvus/minio ./data/milvus/milvus
fi

# Function to install vector store specific packages
install_vector_store_packages() {
  local vector_store=$1
  echo "📦 Installing packages for vector store: $vector_store..."
  
  case "$vector_store" in
    qdrant)
      docker exec openmemory-openmemory-mcp-1 pip install "qdrant-client>=1.9.1" || echo "⚠️ Failed to install qdrant packages"
      ;;
    chroma)
      docker exec openmemory-openmemory-mcp-1 pip install "chromadb>=0.4.24" || echo "⚠️ Failed to install chroma packages"
      ;;
    weaviate)
      docker exec openmemory-openmemory-mcp-1 pip install "weaviate-client>=4.4.0,<4.15.0" || echo "⚠️ Failed to install weaviate packages"
      ;;
    faiss)
      docker exec openmemory-openmemory-mcp-1 pip install "faiss-cpu>=1.7.4" || echo "⚠️ Failed to install faiss packages"
      ;;
    pgvector)
      docker exec openmemory-openmemory-mcp-1 pip install "vecs>=0.4.0" "psycopg>=3.2.8" || echo "⚠️ Failed to install pgvector packages"
      ;;
    redis)
      docker exec openmemory-openmemory-mcp-1 pip install "redis>=5.0.0,<6.0.0" "redisvl>=0.1.0,<1.0.0" || echo "⚠️ Failed to install redis packages"
      ;;
    elasticsearch)
      docker exec openmemory-openmemory-mcp-1 pip install "elasticsearch>=8.0.0,<9.0.0" || echo "⚠️ Failed to install elasticsearch packages"
      ;;
    milvus)
      docker exec openmemory-openmemory-mcp-1 pip install "pymilvus>=2.4.0,<2.6.0" || echo "⚠️ Failed to install milvus packages"
      ;;
    *)
      echo "⚠️ Unknown vector store: $vector_store. Installing default qdrant packages."
      docker exec openmemory-openmemory-mcp-1 pip install "qdrant-client>=1.9.1" || echo "⚠️ Failed to install qdrant packages"
      ;;
  esac
}

# Start services
echo "🚀 Starting backend services..."
docker compose up -d

# Wait for container to be ready before installing packages
echo "⏳ Waiting for container to be ready..."
for i in {1..30}; do
  if docker exec openmemory-openmemory-mcp-1 python -c "import sys; print('ready')" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

# Install vector store specific packages
install_vector_store_packages "$VECTOR_STORE"

# Header X-Admin-Token via arquivo temporário (chmod 600) lido com
# ``curl -H @arquivo`` — o token nunca aparece na linha de comando (``ps``).
ADMIN_HEADER_FILE="$(mktemp)"
chmod 600 "$ADMIN_HEADER_FILE"
trap 'rm -f "$ADMIN_HEADER_FILE"' EXIT
printf 'X-Admin-Token: %s\n' "$ADMIN_TOKEN" > "$ADMIN_HEADER_FILE"

# Prontidão: qualquer resposta HTTP de /api/v1/config (inclusive 401) significa
# "API no ar". Não manda o token (não precisa).
api_is_up() {
  local code
  code="$(curl -sS --connect-timeout 3 --max-time 5 -o /dev/null -w '%{http_code}' "${NEXT_PUBLIC_API_URL}/api/v1/config" 2>/dev/null || true)"
  case "$code" in
    2??|401|403) return 0 ;;
    *) return 1 ;;
  esac
}

wait_for_api() {
  echo "⏳ Waiting for API to be ready at ${NEXT_PUBLIC_API_URL}..."
  for _ in {1..60}; do
    if api_is_up; then
      return 0
    fi
    sleep 1
  done
  echo "⚠️  API did not answer at ${NEXT_PUBLIC_API_URL} after 60s."
  return 1
}

# Grava mem0.vector_store via PUT autenticado. Falha é avisada (não engolida).
seed_vector_store() {
  local store="$1" body="$2" out code
  wait_for_api || true
  echo "🧩 Configuring vector store (${store}) in backend..."
  out="$(mktemp)"
  code="$(curl -sS --connect-timeout 3 --max-time 15 -o "$out" -w '%{http_code}' -X PUT \
    "${NEXT_PUBLIC_API_URL}/api/v1/config/mem0/vector_store" \
    -H @"$ADMIN_HEADER_FILE" \
    -H 'Content-Type: application/json' \
    -d "$body" 2>/dev/null || true)"
  if [ "${code:0:1}" = "2" ]; then
    echo "✅ Vector store (${store}) configured."
  else
    echo "⚠️  WARNING: failed to configure vector store (${store}) — HTTP ${code:-000}."
    case "$code" in
      401|403) echo "   The API rejected ADMIN_TOKEN. Check that the openmemory-mcp container got the same ADMIN_TOKEN (${ADMIN_TOKEN_FILE})." ;;
    esac
    head -c 500 "$out" 2>/dev/null; echo
    echo "   Configure it later in the UI (Configurações) or retry the PUT with X-Admin-Token."
  fi
  rm -f "$out"
}

# If a specific vector store is selected, seed the backend config accordingly
if [ "$VECTOR_STORE" = "milvus" ]; then
  seed_vector_store milvus "{\"provider\":\"milvus\",\"config\":{\"collection_name\":\"openmemory\",\"embedding_model_dims\":${EMBEDDING_DIMS},\"url\":\"http://mem0_store:19530\",\"token\":\"\",\"db_name\":\"\",\"metric_type\":\"COSINE\"}}"
elif [ "$VECTOR_STORE" = "weaviate" ]; then
  seed_vector_store weaviate "{\"provider\":\"weaviate\",\"config\":{\"collection_name\":\"openmemory\",\"embedding_model_dims\":${EMBEDDING_DIMS},\"cluster_url\":\"http://mem0_store:8080\"}}"
elif [ "$VECTOR_STORE" = "redis" ]; then
  seed_vector_store redis "{\"provider\":\"redis\",\"config\":{\"collection_name\":\"openmemory\",\"embedding_model_dims\":${EMBEDDING_DIMS},\"redis_url\":\"redis://mem0_store:6379\"}}"
elif [ "$VECTOR_STORE" = "pgvector" ]; then
  seed_vector_store pgvector "{\"provider\":\"pgvector\",\"config\":{\"collection_name\":\"openmemory\",\"embedding_model_dims\":${EMBEDDING_DIMS},\"dbname\":\"mem0\",\"user\":\"mem0\",\"password\":\"mem0\",\"host\":\"mem0_store\",\"port\":5432,\"diskann\":false,\"hnsw\":true}}"
elif [ "$VECTOR_STORE" = "qdrant" ]; then
  seed_vector_store qdrant "{\"provider\":\"qdrant\",\"config\":{\"collection_name\":\"openmemory\",\"embedding_model_dims\":${EMBEDDING_DIMS},\"host\":\"mem0_store\",\"port\":6333}}"
elif [ "$VECTOR_STORE" = "chroma" ]; then
  seed_vector_store chroma "{\"provider\":\"chroma\",\"config\":{\"collection_name\":\"openmemory\",\"host\":\"mem0_store\",\"port\":8000}}"
elif [ "$VECTOR_STORE" = "elasticsearch" ]; then
  seed_vector_store elasticsearch "{\"provider\":\"elasticsearch\",\"config\":{\"collection_name\":\"openmemory\",\"embedding_model_dims\":${EMBEDDING_DIMS},\"host\":\"http://mem0_store\",\"port\":9200,\"user\":\"elastic\",\"password\":\"changeme\",\"verify_certs\":false,\"use_ssl\":false}}"
elif [ "$VECTOR_STORE" = "faiss" ]; then
  seed_vector_store faiss "{\"provider\":\"faiss\",\"config\":{\"collection_name\":\"openmemory\",\"embedding_model_dims\":${EMBEDDING_DIMS},\"path\":\"/tmp/faiss\",\"distance_strategy\":\"cosine\"}}"
fi

# Start the frontend
echo "🚀 Starting frontend on port $FRONTEND_PORT..."
docker run -d \
  --name mem0_ui \
  -p ${FRONTEND_PORT}:3000 \
  -e NEXT_PUBLIC_API_URL="$NEXT_PUBLIC_API_URL" \
  -e NEXT_PUBLIC_USER_ID="$USER" \
  -e ADMIN_TOKEN \
  mem0/openmemory-ui:latest

echo "✅ Backend:  http://localhost:8765"
echo "✅ Frontend: http://localhost:$FRONTEND_PORT"
echo "🔑 ADMIN_TOKEN: passed to API and UI containers (stored in ${ADMIN_TOKEN_FILE} when generated)."

# Open the frontend URL in the default web browser
echo "🌐 Opening frontend in the default browser..."
URL="http://localhost:$FRONTEND_PORT"

if command -v xdg-open > /dev/null; then
  xdg-open "$URL"        # Linux
elif command -v open > /dev/null; then
  open "$URL"            # macOS
elif command -v start > /dev/null; then
  start "$URL"           # Windows (if run via Git Bash or similar)
else
  echo "⚠️ Could not detect a method to open the browser. Please open $URL manually."
fi