FROM python:3.12-slim

LABEL org.opencontainers.image.name="mem0/openmemory-mcp"

WORKDIR /usr/src/openmemory

# pg_dump/psql para backup/restore do PostgreSQL (task_02 / ADR-003).
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Deps de terceiros (requirements fixa mem0ai pelas deps transitivas; o fork
# in-repo abaixo sombreia o wheel publicado no import).
COPY openmemory/api/requirements.txt .
RUN pip install -r requirements.txt

# Rerank local OPCIONAL (card 80110071): torch CPU + sentence-transformers so
# entram com --build-arg INSTALL_RERANK=1 (~+1,8 GB na imagem: 885 MB -> 2,71 GB). Default 0: imagem
# funcionalmente igual a anterior (so ganha HF_HOME e os requirements-rerank*.txt
# copiados, sem nada instalado). RERANK_PRELOAD_MODEL (ex.:
# cross-encoder/mmarco-mMiniLMv2-L12-H384-v1) baixa o modelo NO BUILD para HF_HOME
# dentro da imagem. Em runtime o loader roda OFFLINE (HF_HUB_OFFLINE=1, ver
# app/utils/reranking.py): modelo fora do cache -> rerank "unavailable", sem rede.
# Por isso o download e liberado (=0) SOMENTE neste RUN. Nenhum volume e tocado.
ARG INSTALL_RERANK=0
ARG RERANK_PRELOAD_MODEL=
ENV HF_HOME=/opt/hf-cache
COPY openmemory/api/requirements-rerank-torch.txt openmemory/api/requirements-rerank.txt ./
# torch em passo proprio, SO do indice CPU (--index-url no arquivo): com o PyPI no
# mesmo resolve (--extra-index-url) o pip podia escolher o wheel CUDA.
RUN if [ "$INSTALL_RERANK" = "1" ]; then \
        pip install --no-cache-dir -r requirements-rerank-torch.txt \
        && pip install --no-cache-dir -r requirements-rerank.txt; \
    fi \
    && if [ "$INSTALL_RERANK" = "1" ] && [ -n "$RERANK_PRELOAD_MODEL" ]; then \
        HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0 HF_HUB_DISABLE_TELEMETRY=1 \
        python -c "import sys; from sentence_transformers import CrossEncoder; CrossEncoder(sys.argv[1], device='cpu')" "$RERANK_PRELOAD_MODEL"; \
    fi

# Vendoriza o fork mem0 do repo e o coloca a frente do site-packages, espelhando
# o CI (openmemory-api-ci.yml seta PYTHONPATH=<raiz> "so import mem0 resolves
# without installing the published mem0ai wheel"). O produto depende de features
# so-do-fork ausentes no mem0ai publico - ex.: add(project=...) / ADR-003.
COPY mem0 /usr/src/mem0
ENV PYTHONPATH=/usr/src

# Codigo da API.
COPY openmemory/api/ .

# Artefatos de infra referenciados pelos testes (compose, stack, scripts).
COPY openmemory/compose/ compose/
COPY openmemory/docker-compose.scale.yml .
COPY openmemory/docker-stack.yml .
COPY openmemory/scripts/ scripts/

EXPOSE 8765
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8765"]
