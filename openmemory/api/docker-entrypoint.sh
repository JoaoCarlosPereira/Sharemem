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
# Escopo da limpeza (card b52c475a): somente arquivos regulares de primeiro
# nível cujo nome segue EXATAMENTE o formato do prometheus_client
# (values.MultiProcessValue, process_identifier padrão = os.getpid()):
#   counter_<pid>.db  histogram_<pid>.db  summary_<pid>.db
#   gauge_<modo>_<pid>.db   (modo ∈ Gauge._MULTIPROC_MODES)
# com <pid> só dígitos. Qualquer outro arquivo (openmemory.db, outro.db, ...)
# é preservado. Symlinks não são seguidos nem removidos.
#
# Recusa (exit 64), antes de tocar em qualquer arquivo:
#   - diretório raiz em qualquer grafia (/, //, /., /./, /tmp/.., symlink p/ /);
#   - o WORKDIR da imagem (/usr/src/openmemory);
#   - qualquer diretório que contenha openmemory.db (SQLite da API).
# Cada serviço deve usar o PRÓPRIO diretório (no compose: tmpfs por
# container), para que um serviço não apague os arquivos de outro.
set -eu

OPENMEMORY_WORKDIR=/usr/src/openmemory

refuse() {
    echo "docker-entrypoint: PROMETHEUS_MULTIPROC_DIR=$1 recusado: $2" >&2
    exit 64
}

# Normalização léxica POSIX (sem realpath): torna absoluto, colapsa barras
# repetidas, descarta componentes "." e resolve ".." sem seguir symlinks.
normalize_path() {
    _np=$1
    case "$_np" in
        /*) ;;
        *) _np="$(pwd)/$_np" ;;
    esac
    _out=""
    _old_ifs=$IFS
    IFS=/
    set -f
    for _c in $_np; do
        case "$_c" in
            '' | .) ;;
            ..) _out=${_out%/*} ;;
            *) _out="$_out/$_c" ;;
        esac
    done
    set +f
    IFS=$_old_ifs
    printf '%s\n' "${_out:-/}"
}

check_forbidden() {
    # $1 = valor original da env (mensagem), $2 = caminho normalizado/resolvido
    case "$2" in
        /) refuse "$1" "diretório raiz" ;;
        "$OPENMEMORY_WORKDIR") refuse "$1" "WORKDIR da aplicação ($OPENMEMORY_WORKDIR)" ;;
    esac
}

is_prometheus_db() {
    _base=${1%.db}
    case "$_base" in
        counter_* | histogram_* | summary_*) _pid=${_base#*_} ;;
        gauge_*)
            _rest=${_base#gauge_}
            _mode=${_rest%_*}
            _pid=${_rest##*_}
            case "$_mode" in
                all | liveall | min | livemin | max | livemax | sum | livesum | mostrecent | livemostrecent) ;;
                *) return 1 ;;
            esac
            ;;
        *) return 1 ;;
    esac
    case "$_pid" in
        '' | *[!0-9]*) return 1 ;;
    esac
    return 0
}

dir="${PROMETHEUS_MULTIPROC_DIR:-}"
if [ -n "$dir" ]; then
    # 1) Checagem léxica antes de criar qualquer coisa.
    check_forbidden "$dir" "$(normalize_path "$dir")"
    mkdir -p "$dir"
    # 2) Caminho físico (resolve symlinks; `pwd -P` é POSIX, existe no dash).
    real=$(cd "$dir" && pwd -P)
    check_forbidden "$dir" "$real"
    if [ -e "$real/openmemory.db" ] || [ -L "$real/openmemory.db" ]; then
        refuse "$dir" "contém openmemory.db (banco SQLite da API)"
    fi
    for f in "$real"/*.db; do
        [ -f "$f" ] && [ ! -L "$f" ] || continue
        if is_prometheus_db "${f##*/}"; then
            rm -f -- "$f"
        fi
    done
fi

exec "$@"
