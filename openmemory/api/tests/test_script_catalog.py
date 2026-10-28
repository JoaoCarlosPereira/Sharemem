import copy
import datetime
import logging
import threading
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.models import ScriptDownload
from app.services.script_catalog import ScriptCatalog
from app.services.script_packages import ScriptInput, ScriptRuleError, content_sha256
from app.utils.agentregistry import (
    AgentRegistryError,
    AgentRegistryRateLimited,
    AgentRegistryResourceNotFound,
)

NAME = "gcenfe02-xml"
AUTH = {"Authorization": "Bearer omtk_token"}


class FakeRegistry:
    def __init__(self, log):
        self.store = {}
        self.calls = log
        self.error = None

    def _maybe_fail(self):
        if self.error:
            raise self.error

    async def get_resource(self, *, kind, name, tag=None, namespace="default", auth_headers=None):
        self.calls.append(("get", kind, name, tag, auth_headers))
        self._maybe_fail()
        key = (name, tag or "latest")
        if key not in self.store:
            raise AgentRegistryResourceNotFound()
        return copy.deepcopy(self.store[key])

    async def list_tags(self, *, kind, name, namespace="default", auth_headers=None):
        self.calls.append(("tags", kind, name, None, auth_headers))
        self._maybe_fail()
        items = [copy.deepcopy(res) for (stored, _), res in self.store.items() if stored == name]
        return {"items": items}

    async def apply_resource(self, *, resource, dry_run=False, auth_headers=None):
        tag = resource["metadata"]["tag"]
        self.calls.append(("apply", resource["kind"], resource["metadata"]["name"], tag, auth_headers))
        self._maybe_fail()
        self.store[(resource["metadata"]["name"], tag)] = copy.deepcopy(resource)
        return {"results": [{"status": "created"}]}


def _db(log, dialect="sqlite"):
    db = MagicMock()
    db.get_bind.return_value.dialect.name = dialect
    db.execute.side_effect = lambda stmt, params=None: log.append(("execute", str(stmt), params))
    db.rollback.side_effect = lambda: log.append(("end_transaction",))
    db.commit.side_effect = lambda: log.append(("end_transaction",))
    db.scalar.return_value = 0
    return db


def _script(**overrides):
    payload = {
        "name": NAME,
        "file_name": "gcenfe02_xml.py",
        "title": "Decodificador GCENFE02",
        "description": "Decodifica a coluna GCENFE02.XML",
        "usage": "python gcenfe02_xml.py <arquivo>",
        "language": "python",
        "requirements": ["python>=3.8"],
        "effects": ["somente-leitura"],
        "area": "sysmo-s1",
        "tags": ["nfe"],
        "content": "print('v1')\n",
    }
    payload.update(overrides)
    return ScriptInput(**payload)


def _catalog(dialect="sqlite"):
    log = []
    registry = FakeRegistry(log)
    return ScriptCatalog(registry, _db(log, dialect), AUTH), registry, log


def _registry_calls(log):
    return [entry for entry in log if entry[0] in ("get", "tags", "apply")]


def _sqlite_catalog():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    ScriptDownload.__table__.create(engine)
    registry = FakeRegistry([])
    return ScriptCatalog(registry, Session(engine), AUTH), registry, engine


@pytest.mark.asyncio
async def test_scr01_first_publication_creates_version_1_and_latest():
    catalog, registry, log = _catalog()
    script = _script()

    result = await catalog.publish(script, "pessoa-1")

    assert result.model_dump() == {
        "name": NAME,
        "version": "1",
        "sha256": content_sha256(script.content),
        "created": True,
    }
    assert set(registry.store) == {(NAME, "1"), (NAME, "latest")}
    for tag in ("1", "latest"):
        resource = registry.store[(NAME, tag)]
        annotations = resource["metadata"]["annotations"]
        assert resource["kind"] == "Script"
        assert resource["spec"]["content"] == script.content
        assert annotations["sharemem.io/versao"] == "1"
        assert annotations["sharemem.io/autor"] == "pessoa-1"
        assert annotations["sharemem.io/sha256"] == content_sha256(script.content)
        published = datetime.datetime.fromisoformat(annotations["sharemem.io/publicado-em"])
        assert published.utcoffset() == datetime.timedelta(0)
    calls = _registry_calls(log)
    assert len(calls) <= 4
    assert [call[3] for call in calls if call[0] == "apply"] == ["1", "latest"]
    assert all(call[4] == AUTH for call in calls)


@pytest.mark.asyncio
async def test_scr15_new_content_creates_next_version_and_moves_latest():
    catalog, registry, _ = _catalog()
    await catalog.publish(_script(), "pessoa-1")

    result = await catalog.publish(_script(content="print('v2')\n"), "pessoa-2")

    assert (result.version, result.created) == ("2", True)
    latest = registry.store[(NAME, "latest")]
    assert latest["spec"]["content"] == "print('v2')\n"
    assert latest["metadata"]["annotations"]["sharemem.io/versao"] == "2"
    assert latest["metadata"]["annotations"]["sharemem.io/autor"] == "pessoa-2"
    assert registry.store[(NAME, "1")]["spec"]["content"] == "print('v1')\n"


@pytest.mark.asyncio
async def test_scr15_next_version_is_highest_numeric_tag_plus_one():
    catalog, registry, _ = _catalog()
    await catalog.publish(_script(), "pessoa-1")
    ninth = copy.deepcopy(registry.store[(NAME, "1")])
    ninth["metadata"]["tag"] = "9"
    registry.store[(NAME, "9")] = ninth

    result = await catalog.publish(_script(content="print('v10')\n"), "pessoa-1")

    assert result.version == "10"


@pytest.mark.asyncio
async def test_scr16_identical_republication_does_not_apply():
    catalog, _, log = _catalog()
    await catalog.publish(_script(), "pessoa-1")
    await catalog.publish(_script(content="print('v2')\n"), "pessoa-1")
    log.clear()

    result = await catalog.publish(_script(content="print('v2')\n"), "pessoa-3")

    assert [call for call in log if call[0] == "apply"] == []
    assert (result.version, result.created) == ("2", False)
    assert result.sha256 == content_sha256("print('v2')\n")


@pytest.mark.asyncio
async def test_scr16_same_content_with_different_card_creates_version():
    catalog, _, _ = _catalog()
    await catalog.publish(_script(), "pessoa-1")

    result = await catalog.publish(_script(usage="python gcenfe02_xml.py -h"), "pessoa-1")

    assert (result.version, result.created) == ("2", True)


@pytest.mark.asyncio
async def test_scr18_postgresql_takes_lock_before_reading_tags():
    catalog, _, log = _catalog(dialect="postgresql")

    await catalog.publish(_script(), "pessoa-1")

    kinds = [entry[0] for entry in log]
    assert kinds.index("execute") < kinds.index("tags")
    _, sql, params = log[kinds.index("execute")]
    assert "pg_advisory_xact_lock(hashtext(:key))" in sql
    assert params == {"key": f"script_publish:{NAME}"}
    assert kinds.index("tags") < kinds.index("apply")


@pytest.mark.asyncio
async def test_scr18_lock_is_released_with_the_transaction_after_applying():
    catalog, _, log = _catalog(dialect="postgresql")

    await catalog.publish(_script(), "pessoa-1")

    kinds = [entry[0] for entry in log]
    last_apply = len(kinds) - 1 - kinds[::-1].index("apply")
    assert kinds.count("end_transaction") == 1
    assert kinds.index("execute") < last_apply < kinds.index("end_transaction")


@pytest.mark.asyncio
async def test_scr18_lock_wait_runs_off_the_event_loop_thread():
    catalog, _, _ = _catalog(dialect="postgresql")
    threads = []
    catalog.db.execute.side_effect = lambda stmt, params=None: threads.append(threading.get_ident())

    await catalog.publish(_script(), "pessoa-1")

    assert threads and threads[0] != threading.get_ident()


@pytest.mark.asyncio
async def test_scr18_sqlite_does_not_take_lock():
    catalog, _, log = _catalog(dialect="sqlite")

    await catalog.publish(_script(), "pessoa-1")

    assert [entry for entry in log if entry[0] == "execute"] == []


@pytest.mark.asyncio
async def test_scr10_get_without_version_returns_latest_with_card_and_sha():
    catalog, _, log = _catalog()
    await catalog.publish(_script(), "pessoa-1")
    await catalog.publish(_script(content="print('v2')\n"), "pessoa-2")
    log.clear()

    view = await catalog.get(NAME)

    assert view.name == NAME
    assert view.version == "2"
    assert view.is_latest is True
    assert view.file_name == "gcenfe02_xml.py"
    assert view.content == "print('v2')\n"
    assert view.sha256 == content_sha256("print('v2')\n")
    assert view.warning is None
    card = view.card
    assert card.title == "Decodificador GCENFE02"
    assert card.description == "Decodifica a coluna GCENFE02.XML"
    assert card.usage == "python gcenfe02_xml.py <arquivo>"
    assert card.language == "python"
    assert card.requirements == ["python>=3.8"]
    assert card.effects == ["somente-leitura"]
    assert card.area == "sysmo-s1"
    assert card.tags == ["nfe"]
    assert card.author == "pessoa-2"
    assert card.published_at
    assert len(_registry_calls(log)) == 2


@pytest.mark.asyncio
async def test_scr11_get_fixed_version_returns_exact_content():
    catalog, _, _ = _catalog()
    await catalog.publish(_script(), "pessoa-1")
    await catalog.publish(_script(content="print('v2')\n"), "pessoa-2")

    view = await catalog.get(NAME, "1")

    assert view.content == "print('v1')\n"
    assert view.version == "1"
    assert view.is_latest is False
    assert view.card.author == "pessoa-1"


@pytest.mark.asyncio
async def test_scr12_missing_version_is_reported_with_name_and_version():
    catalog, _, _ = _catalog()

    with pytest.raises(ScriptRuleError) as exc:
        await catalog.get("nao-existe", "9")

    assert str(exc.value) == "Script não encontrado: nao-existe@9"


@pytest.mark.asyncio
async def test_scr12_missing_script_without_version_reports_latest():
    catalog, _, _ = _catalog()

    with pytest.raises(ScriptRuleError) as exc:
        await catalog.get("nao-existe")

    assert str(exc.value) == "Script não encontrado: nao-existe@latest"


@pytest.mark.asyncio
async def test_scr13_view_carries_effect_warning():
    catalog, _, _ = _catalog()
    await catalog.publish(_script(effects=["altera-banco"]), "pessoa-1")

    view = await catalog.get(NAME)

    assert view.warning == "Este script altera-banco; confirme com o usuário antes de rodar"


@pytest.mark.asyncio
async def test_scr14_content_round_trips_byte_for_byte_with_crlf_and_accents():
    catalog, _, _ = _catalog()
    content = "# ação\r\nprint('é')\r\n"

    result = await catalog.publish(_script(content=content), "pessoa-1")
    view = await catalog.get(NAME, "1")

    assert view.content.encode("utf-8") == content.encode("utf-8")
    assert view.sha256 == result.sha256 == content_sha256(content)


@pytest.mark.asyncio
async def test_scr29_rollback_points_latest_to_old_version_and_keeps_newer():
    catalog, registry, _ = _catalog()
    await catalog.publish(_script(), "pessoa-1")
    await catalog.publish(_script(content="print('v2')\n"), "pessoa-1")

    view = await catalog.rollback(NAME, "1", "pessoa-2")

    assert view.version == "1"
    assert view.is_latest is True
    assert view.content == "print('v1')\n"
    latest = registry.store[(NAME, "latest")]
    assert latest["spec"]["content"] == "print('v1')\n"
    annotations = latest["metadata"]["annotations"]
    assert annotations["sharemem.io/versao"] == "1"
    assert annotations["sharemem.io/autor"] == "pessoa-2"
    assert annotations["sharemem.io/sha256"] == content_sha256("print('v1')\n")
    rolled_at = datetime.datetime.fromisoformat(annotations["sharemem.io/publicado-em"])
    first_at = datetime.datetime.fromisoformat(
        registry.store[(NAME, "1")]["metadata"]["annotations"]["sharemem.io/publicado-em"]
    )
    assert rolled_at >= first_at
    assert registry.store[(NAME, "1")]["metadata"]["annotations"]["sharemem.io/autor"] == "pessoa-1"
    assert (await catalog.get(NAME)).content == "print('v1')\n"
    assert (await catalog.get(NAME, "2")).content == "print('v2')\n"
    assert {(NAME, "1"), (NAME, "2"), (NAME, "latest")} == set(registry.store)


@pytest.mark.asyncio
async def test_scr16_republishing_rolled_back_version_keeps_latest_version():
    catalog, _, log = _catalog()
    await catalog.publish(_script(), "pessoa-1")
    await catalog.publish(_script(content="print('v2')\n"), "pessoa-1")
    await catalog.rollback(NAME, "1", "pessoa-2")
    log.clear()

    result = await catalog.publish(_script(), "pessoa-3")

    assert (result.version, result.created) == ("1", False)
    assert [entry for entry in log if entry[0] == "apply"] == []


@pytest.mark.asyncio
async def test_scr18_rollback_takes_publish_lock_before_reading_tag():
    catalog, _, log = _catalog(dialect="postgresql")
    await catalog.publish(_script(), "pessoa-1")
    await catalog.publish(_script(content="print('v2')\n"), "pessoa-1")
    log.clear()

    await catalog.rollback(NAME, "1", "pessoa-2")

    kinds = [entry[0] for entry in log]
    _, sql, params = log[kinds.index("execute")]
    assert "pg_advisory_xact_lock(hashtext(:key))" in sql
    assert params == {"key": f"script_publish:{NAME}"}
    assert kinds.index("execute") < kinds.index("get") < kinds.index("apply")
    assert kinds.index("apply") < kinds.index("end_transaction")


@pytest.mark.asyncio
async def test_scr18_rollback_lock_wait_runs_off_the_event_loop_thread():
    catalog, _, _ = _catalog(dialect="postgresql")
    await catalog.publish(_script(), "pessoa-1")
    threads = []
    catalog.db.execute.side_effect = lambda stmt, params=None: threads.append(threading.get_ident())

    await catalog.rollback(NAME, "1", "pessoa-2")

    assert threads and threads[0] != threading.get_ident()


@pytest.mark.asyncio
async def test_scr18_rollback_with_sqlite_does_not_take_lock():
    catalog, _, log = _catalog(dialect="sqlite")
    await catalog.publish(_script(), "pessoa-1")

    await catalog.rollback(NAME, "1", "pessoa-2")

    assert [entry for entry in log if entry[0] == "execute"] == []


@pytest.mark.asyncio
async def test_scr29_rollback_to_missing_version_is_reported():
    catalog, _, _ = _catalog()
    await catalog.publish(_script(), "pessoa-1")

    with pytest.raises(ScriptRuleError) as exc:
        await catalog.rollback(NAME, "9", "pessoa-2")

    assert str(exc.value) == f"Script não encontrado: {NAME}@9"


@pytest.mark.asyncio
async def test_scr31_versions_list_newest_first_and_marks_latest_after_rollback():
    catalog, registry, _ = _catalog()
    await catalog.publish(_script(), "pessoa-1")
    await catalog.publish(_script(content="print('v2')\n"), "pessoa-2")
    await catalog.rollback(NAME, "1", "pessoa-3")

    view = await catalog.get(NAME)

    published = {
        tag: registry.store[(NAME, tag)]["metadata"]["annotations"]["sharemem.io/publicado-em"]
        for tag in ("1", "2")
    }
    assert [version.model_dump() for version in view.versions] == [
        {"version": "2", "author": "pessoa-2", "published_at": published["2"], "is_latest": False},
        {"version": "1", "author": "pessoa-1", "published_at": published["1"], "is_latest": True},
    ]


@pytest.mark.asyncio
async def test_scr25_connection_failure_becomes_store_unavailable():
    catalog, registry, _ = _catalog()
    registry.error = AgentRegistryError(502, "AgentRegistry indisponível")

    with pytest.raises(ScriptRuleError) as get_exc:
        await catalog.get(NAME)
    with pytest.raises(ScriptRuleError) as publish_exc:
        await catalog.publish(_script(), "pessoa-1")

    assert str(get_exc.value) == "Store indisponível; tente de novo em instantes"
    assert str(publish_exc.value) == "Store indisponível; tente de novo em instantes"


@pytest.mark.asyncio
async def test_scr26_rate_limit_reports_wait_seconds():
    catalog, registry, _ = _catalog()
    registry.error = AgentRegistryRateLimited("7")

    with pytest.raises(ScriptRuleError) as exc:
        await catalog.get(NAME)

    assert str(exc.value) == "Limite da Store atingido; tente em 7 s"


@pytest.mark.asyncio
async def test_scr26_rate_limit_without_retry_after_asks_to_retry_shortly():
    catalog, registry, _ = _catalog()
    registry.error = AgentRegistryRateLimited(None)

    with pytest.raises(ScriptRuleError) as exc:
        await catalog.get(NAME)

    assert str(exc.value) == "Limite da Store atingido; tente de novo em instantes"


@pytest.mark.asyncio
async def test_publish_and_rollback_are_logged(caplog):
    catalog, _, _ = _catalog()
    caplog.set_level(logging.INFO)

    await catalog.publish(_script(), "pessoa-1")
    await catalog.publish(_script(), "pessoa-1")
    await catalog.rollback(NAME, "1", "pessoa-2")

    messages = [record.getMessage() for record in caplog.records]
    assert f"script_publish name={NAME} version=1 created=True author=pessoa-1" in messages
    assert f"script_publish name={NAME} version=1 created=False author=pessoa-1" in messages
    assert f"script_rollback name={NAME} version=1 author=pessoa-2" in messages


@pytest.mark.asyncio
async def test_scr19_retire_marks_only_latest_obsolete_and_keeps_spec():
    catalog, registry, log = _catalog()
    await catalog.publish(_script(), "pessoa-1")
    published_spec = copy.deepcopy(registry.store[(NAME, "latest")]["spec"])
    log.clear()

    view = await catalog.retire(NAME, "pessoa-2")

    latest = registry.store[(NAME, "latest")]
    annotations = latest["metadata"]["annotations"]
    assert annotations["sharemem.io/obsoleto"] == "true"
    assert annotations["sharemem.io/versao"] == "1"
    assert annotations["sharemem.io/autor"] == "pessoa-2"
    assert latest["spec"] == published_spec
    assert "sharemem.io/obsoleto" not in registry.store[(NAME, "1")]["metadata"]["annotations"]
    assert [call[3] for call in _registry_calls(log) if call[0] == "apply"] == ["latest"]
    assert (view.version, view.warning) == ("1", "Script obsoleto")


@pytest.mark.asyncio
async def test_scr19_retired_script_is_still_delivered_with_warning():
    catalog, _, _ = _catalog()
    await catalog.publish(_script(), "pessoa-1")
    await catalog.retire(NAME, "pessoa-2")

    latest = await catalog.get(NAME)
    fixed = await catalog.get(NAME, "1")

    assert (latest.content, latest.warning) == ("print('v1')\n", "Script obsoleto")
    assert (fixed.content, fixed.warning) == ("print('v1')\n", "Script obsoleto")


@pytest.mark.asyncio
async def test_scr19_retired_warning_keeps_effect_warning():
    catalog, _, _ = _catalog()
    await catalog.publish(_script(effects=["altera-banco"]), "pessoa-1")
    await catalog.retire(NAME, "pessoa-2")

    view = await catalog.get(NAME)

    assert view.warning == (
        "Script obsoleto. Este script altera-banco; confirme com o usuário antes de rodar"
    )


@pytest.mark.asyncio
async def test_scr19_publishing_new_version_clears_obsolete_mark():
    catalog, registry, _ = _catalog()
    await catalog.publish(_script(), "pessoa-1")
    await catalog.retire(NAME, "pessoa-2")

    result = await catalog.publish(_script(content="print('v2')\n"), "pessoa-3")
    view = await catalog.get(NAME)

    assert (result.version, result.created) == ("2", True)
    assert "sharemem.io/obsoleto" not in registry.store[(NAME, "latest")]["metadata"]["annotations"]
    assert view.warning is None


@pytest.mark.asyncio
async def test_scr19_retire_takes_publish_lock_before_reading_latest():
    catalog, _, log = _catalog(dialect="postgresql")
    await catalog.publish(_script(), "pessoa-1")
    log.clear()

    await catalog.retire(NAME, "pessoa-2")

    kinds = [entry[0] for entry in log]
    _, sql, params = log[kinds.index("execute")]
    assert "pg_advisory_xact_lock(hashtext(:key))" in sql
    assert params == {"key": f"script_publish:{NAME}"}
    assert kinds.index("execute") < kinds.index("get") < kinds.index("apply")
    assert kinds.index("apply") < kinds.index("end_transaction")


@pytest.mark.asyncio
async def test_scr19_retire_missing_script_is_reported():
    catalog, _, _ = _catalog()

    with pytest.raises(ScriptRuleError) as exc:
        await catalog.retire(NAME, "pessoa-2")

    assert str(exc.value) == f"Script não encontrado: {NAME}@latest"


@pytest.mark.asyncio
async def test_retire_is_logged(caplog):
    catalog, _, _ = _catalog()
    await catalog.publish(_script(), "pessoa-1")
    caplog.set_level(logging.INFO)

    await catalog.retire(NAME, "pessoa-2")

    messages = [record.getMessage() for record in caplog.records]
    assert f"script_retire name={NAME} author=pessoa-2" in messages


@pytest.mark.asyncio
async def test_scr23_get_records_name_version_hostname_and_time():
    catalog, _, _ = _sqlite_catalog()
    await catalog.publish(_script(), "pessoa-1")
    await catalog.publish(_script(content="print('v2')\n"), "pessoa-1")
    before = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

    await catalog.get(NAME, "1", hostname="maquina-a")

    rows = catalog.db.scalars(select(ScriptDownload)).all()
    assert [(row.name, row.version, row.hostname) for row in rows] == [(NAME, "1", "maquina-a")]
    downloaded_at = rows[0].downloaded_at.replace(tzinfo=None)
    assert before - datetime.timedelta(seconds=1) <= downloaded_at <= before + datetime.timedelta(minutes=1)


@pytest.mark.asyncio
async def test_scr24_two_downloads_from_different_machines_count_two():
    catalog, _, _ = _sqlite_catalog()
    await catalog.publish(_script(), "pessoa-1")

    await catalog.get(NAME, hostname="maquina-a")
    view = await catalog.get(NAME, hostname="maquina-b")

    assert view.card.downloads_30d == 2


@pytest.mark.asyncio
async def test_scr24_download_older_than_30_days_is_not_counted():
    catalog, _, _ = _sqlite_catalog()
    await catalog.publish(_script(), "pessoa-1")
    old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=31)
    catalog.db.add(ScriptDownload(name=NAME, version="1", hostname="maquina-a", downloaded_at=old))
    catalog.db.add(ScriptDownload(name="outro-script", version="1", hostname="maquina-a"))
    catalog.db.commit()

    view = await catalog.get(NAME, hostname="maquina-b")

    assert view.card.downloads_30d == 1


@pytest.mark.asyncio
async def test_scr24_download_count_is_a_single_select():
    catalog, _, engine = _sqlite_catalog()
    await catalog.publish(_script(), "pessoa-1")
    statements = []
    event.listen(engine, "before_cursor_execute", lambda *args: statements.append(args[2]))

    await catalog.get(NAME, hostname="maquina-a")

    selects = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
    inserts = [sql for sql in statements if sql.lstrip().upper().startswith("INSERT")]
    assert len(selects) == 1 and "count(" in selects[0].lower()
    assert len(inserts) == 1


@pytest.mark.asyncio
async def test_scr23_failed_insert_does_not_block_get(caplog):
    catalog, _, engine = _sqlite_catalog()
    await catalog.publish(_script(), "pessoa-1")
    ScriptDownload.__table__.drop(engine)

    view = await catalog.get(NAME, hostname="maquina-a")

    assert view.content == "print('v1')\n"
    assert view.card.downloads_30d is None
    assert any(
        record.levelname == "ERROR" and f"script_download name={NAME}" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_scr23_failed_insert_keeps_count_of_previous_downloads(monkeypatch):
    catalog, _, _ = _sqlite_catalog()
    await catalog.publish(_script(), "pessoa-1")
    await catalog.get(NAME, hostname="maquina-a")

    def failing_commit():
        raise OperationalError("INSERT", {}, Exception("banco fora"))

    monkeypatch.setattr(catalog.db, "commit", failing_commit)
    view = await catalog.get(NAME, hostname="maquina-b")

    assert view.content == "print('v1')\n"
    assert view.card.downloads_30d == 1


@pytest.mark.asyncio
async def test_scr23_get_without_hostname_does_not_record():
    catalog, _, _ = _sqlite_catalog()
    await catalog.publish(_script(), "pessoa-1")

    view = await catalog.get(NAME)

    assert catalog.db.scalars(select(ScriptDownload)).all() == []
    assert view.card.downloads_30d == 0
