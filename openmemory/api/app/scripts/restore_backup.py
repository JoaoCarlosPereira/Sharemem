"""CLI one-shot de restore na instalação (task_07 / ADR-004).

Executado em container one-shot pelo ``bootstrap-scale.sh --restore-from``::

    python -m app.scripts.restore_backup /restore/backup.zip

Restaura o estado (PostgreSQL → Qdrant) a partir de um ``.zip`` de backup, SEM
snapshot de segurança (ambiente novo/vazio — ver ADR-004). Reutiliza a MESMA
função de restore da UI (``BackupArchive.restore``). Sai com código != 0 quando o
arquivo não existe, está corrompido ou é de versão incompatível.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Callable, Optional

from app.database import SessionLocal
from app.utils.backup import BackupService
from app.utils.backup_archive import (
    ArchiveCorruptError,
    BackupArchive,
    SchemaIncompatibleError,
)
from app.utils.backup_policy import get_backup_policy_runtime
from app.utils.logging_context import configure_process_logging

logger = logging.getLogger(__name__)


def _default_build() -> BackupArchive:
    db = SessionLocal()
    try:
        policy = get_backup_policy_runtime(db)
    finally:
        db.close()
    return BackupArchive(BackupService(), policy)


def run_restore(
    archive_path: str, *, build: Optional[Callable[[], BackupArchive]] = None
) -> dict:
    """Aplica o restore (sem snapshot de segurança) a partir do ``.zip``."""
    archive = (build or _default_build)()
    return archive.restore(archive_path, safety_snapshot=False)


def main(argv=None, *, build: Optional[Callable[[], BackupArchive]] = None) -> int:
    configure_process_logging()
    parser = argparse.ArgumentParser(description="Restore de backup na instalação.")
    parser.add_argument("archive", help="caminho do arquivo .zip de backup")
    args = parser.parse_args(argv)
    try:
        result = run_restore(args.archive, build=build)
    except FileNotFoundError:
        print(f"ERRO: arquivo de backup não encontrado: {args.archive}", file=sys.stderr)
        return 2
    except SchemaIncompatibleError as exc:
        print(f"ERRO: backup incompatível com esta versão: {exc}", file=sys.stderr)
        return 3
    except ArchiveCorruptError as exc:
        print(f"ERRO: backup corrompido/ inválido: {exc}", file=sys.stderr)
        return 4
    logger.info("restore concluído: %s", result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
