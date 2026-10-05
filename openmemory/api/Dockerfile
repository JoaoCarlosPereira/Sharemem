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
# Limpa PROMETHEUS_MULTIPROC_DIR (se definido) antes de qualquer processo
# Python e então executa o CMD/command do compose. Invocado via `sh` para não
# depender do bit executável preservado no checkout.
ENTRYPOINT ["sh", "/usr/src/openmemory/docker-entrypoint.sh"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8765"]
