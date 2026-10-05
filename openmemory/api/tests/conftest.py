"""Shared pytest hooks for OpenMemory API tests."""

import datetime
import re

import pytest

_DML_VERBS = ("insert", "update", "delete")
_DML_RE = re.compile(r"\b(insert|update|delete)\b")

# ``datetime.UTC`` exists only on Python 3.11+; CI still runs 3.10.
if not hasattr(datetime, "UTC"):
    datetime.UTC = datetime.timezone.utc  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def _isolate_production_dotenv(monkeypatch):
    """Keep unit tests independent of a developer/production ``api/.env``."""
    monkeypatch.delenv("OPENMEMORY_DISCOVERY_BASE_URL", raising=False)


# --------------------------------------------------------------------------- #
# Relatório do autodedup: nunca gravar no banco real
#
# ``MEM0_AUTODEDUP_MODE=report`` grava em ``autodedup_reports`` numa sessão
# própria (``app.database.SessionLocal`` = DATABASE_URL ou ./openmemory.db) e
# roda retenção (DELETE). Qualquer teste que acione esse caminho — direto ou via
# write-worker — sujaria o banco do desenvolvedor. Aqui a fábrica padrão é
# redirecionada para um SQLite em memória por teste, e um listener no engine
# real faz o teste falhar se, mesmo assim, houver DML nessa tabela.
# --------------------------------------------------------------------------- #
class AutodedupReportSandbox:
    """SQLite em memória (criado sob demanda) que recebe as gravações do report."""

    def __init__(self):
        self._engine = None
        self._factory = None
        self.violations: list[str] = []

    def guard(self, conn, cursor, statement, parameters, context, executemany):
        """``before_cursor_execute`` no engine REAL: anota DML em autodedup_reports."""
        sql = " ".join(str(statement).split()).lower()
        if "autodedup_reports" not in sql:
            return
        verb = sql.split(" ", 1)[0]
        # ``WITH ... INSERT/UPDATE/DELETE`` (CTE) também é DML.
        if verb in _DML_VERBS or (verb == "with" and _DML_RE.search(sql)):
            self.violations.append(sql[:200])

    @property
    def factory(self):
        if self._factory is None:
            from sqlalchemy import create_engine
            from sqlalchemy.orm import sessionmaker
            from sqlalchemy.pool import StaticPool

            from app.models import AutodedupReport

            self._engine = create_engine(
                "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
            )
            AutodedupReport.__table__.create(bind=self._engine)
            self._factory = sessionmaker(autocommit=False, autoflush=False, bind=self._engine)
        return self._factory

    def count(self) -> int:
        if self._factory is None:
            return 0
        from app.models import AutodedupReport

        db = self._factory()
        try:
            return db.query(AutodedupReport).count()
        finally:
            db.close()

    def dispose(self):
        if self._engine is not None:
            self._engine.dispose()


@pytest.fixture(autouse=True)
def autodedup_report_sandbox(monkeypatch):
    """Redireciona a gravação padrão do relatório para fora do banco real.

    Testes que já trocam ``app.database.SessionLocal`` (ex.:
    ``test_autodedup_report.py``) continuam recebendo a fábrica deles.
    """
    import app.database as database
    from app.utils import autodedup_report

    from sqlalchemy import event

    real_session_local = database.SessionLocal
    real_engine = database.engine
    sandbox = AutodedupReportSandbox()

    def _factory():
        current = database.SessionLocal
        return sandbox.factory if current is real_session_local else current

    monkeypatch.setattr(autodedup_report, "_default_session_factory", _factory)
    monkeypatch.setattr(autodedup_report, "_last_prune_monotonic", None)

    event.listen(real_engine, "before_cursor_execute", sandbox.guard)
    try:
        yield sandbox
    finally:
        event.remove(real_engine, "before_cursor_execute", sandbox.guard)
        sandbox.dispose()
    if sandbox.violations:
        pytest.fail(
            "teste gravou em autodedup_reports no banco REAL (DATABASE_URL): "
            + "; ".join(sandbox.violations)
        )
