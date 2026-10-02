#!/bin/sh
# Entrypoint da imagem mem0/openmemory-mcp (API e workers).
#
# Modo multiprocesso do prometheus_client (opt-in): quando
# PROMETHEUS_MULTIPROC_DIR está definido, cada processo Python grava seus
# valores em arquivos <tipo>_<pid>.db nesse diretório e /metrics os agrega.
# Arquivos de uma execução anterior do container (PIDs reaproveitados,
# contadores antigos) distorceriam o agregado, então o diretório é limpo AQUI,
# antes de qualquer processo Python (alembic, uvicorn, workers) criar arquivos.
#
# Escopo da limpeza: somente arquivos *.db de primeiro nível do diretório
# configurado — nunca volumes de dados. Cada serviço deve usar o PRÓPRIO
# diretório (no compose: tmpfs por container), para que um serviço não apague
# os arquivos de outro.
set -eu

dir="${PROMETHEUS_MULTIPROC_DIR:-}"
if [ -n "$dir" ]; then
    case "$dir" in
        /) echo "docker-entrypoint: PROMETHEUS_MULTIPROC_DIR=/ recusado" >&2; exit 64 ;;
    esac
    mkdir -p "$dir"
    find "$dir" -mindepth 1 -maxdepth 1 -type f -name '*.db' -exec rm -f {} +
fi

exec "$@"
