import copy
import datetime
import hashlib
import logging
import threading
from unittest.mock import MagicMock

import pytest

from app.services.app_catalog import AppCatalog
from app.services.app_packages import AppInput, AppRuleError, build_app_archive
from app.utils.agentregistry import (
    AgentRegistryError,
    AgentRegistryRateLimited,
    AgentRegistryResourceNotFound,
)
from tests.test_app_packages import WHEEL_NAME, _card, _wheel

NAME = "horas-app"
AUTH = {"Authorization": "Bearer omtk_token"}
WHEEL_V1 = _wheel()
WHEEL_V2 = _wheel(extra={"horas_app/novo.py": "print('v2')"})
SHA_V1 = hashlib.sha256(WHEEL_V1).hexdigest()
SHA_V2 = hashlib.sha256(WHEEL_V2).hexdigest()


class FakeRegistry:
    def __init__(self, log):
        self.store = {}
        self.artifacts = {}
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

    async def put_package_artifact(self, *, kind, name, tag, archive, namespace="default", auth_headers=None):
        self.calls.append(("put", kind, name, tag, auth_headers))
        self._maybe_fail()
        self.artifacts[(name, tag)] = archive
        return {"status": "created"}

    async def get_package_artifact(self, *, kind, name, tag, namespace="default", auth_headers=None):
        self.calls.append(("artifact", kind, name, tag, auth_headers))
        self._maybe_fail()
        if (name, tag) not in self.artifacts:
            raise AgentRegistryResourceNotFound()
        return self.artifacts[(name, tag)], {}


def _db(log, dialect="sqlite"):
    db = MagicMock()
    db.get_bind.return_value.dialect.name = dialect
    db.execute.side_effect = lambda stmt, params=None: log.append(("execute", str(stmt), params))
    db.rollback.side_effect = lambda: log.append(("end_transaction",))
    db.commit.side_effect = lambda: log.append(("end_transaction",))
    return db


def _app(wheel=None, **card_changes):
    return AppInput.from_upload(_card(**card_changes), WHEEL_NAME, WHEEL_V1 if wheel is None else wheel)


def _catalog(dialect="sqlite"):
    log = []
    registry = FakeRegistry(log)
    return AppCatalog(registry, _db(log, dialect), AUTH), registry, log


def _registry_calls(log):
    return [entry for entry in log if entry[0] in ("get", "tags", "apply", "put", "artifact")]


@pytest.mark.asyncio
async def test_app01_first_publication_applies_resource_and_artifact_in_1_and_latest():
    catalog, registry, log = _catalog()
    app = _app()
    archive, _ = build_app_archive(app)

    result = await catalog.publish(app, "pessoa-1")

    assert result.model_dump() == {"name": NAME, "version": "1", "sha256": SHA_V1, "created": True}
    assert set(registry.store) == {(NAME, "1"), (NAME, "latest")}
    assert registry.artifacts == {(NAME, "1"): archive, (NAME, "latest"): archive}
    for tag in ("1", "latest"):
        resource = registry.store[(NAME, tag)]
        annotations = resource["metadata"]["annotations"]
        assert resource["apiVersion"] == "ar.dev/v1alpha1"
        assert resource["kind"] == "LocalApp"
        assert resource["metadata"]["name"] == NAME
        assert resource["metadata"]["tag"] == tag
        assert annotations["sharemem.io/versao"] == "1"
        assert annotations["sharemem.io/autor"] == "pessoa-1"
        assert annotations["sharemem.io/sha256"] == SHA_V1
        published = datetime.datetime.fromisoformat(annotations["sharemem.io/publicado-em"])
        assert published.utcoffset() == datetime.timedelta(0)
    assert registry.store[(NAME, "latest")]["spec"] == {
        "title": "Controle de horas",
        "description": "Controle de horas local com lançamento no Redmine.",
        "usage": "Inicie com `horas-app` e confira com `horas-app verificar`.",
        "requirements": ["Python 3.11 ou mais"],
        "effects": ["grava-arquivos", "acessa-rede"],
        "area": "redmine",
        "tags": ["horas"],
        "wheel": WHEEL_NAME,
        "pythonRequires": ">=3.11",
        "commands": {"start": "horas-app", "verify": "horas-app verificar"},
        "settings": [
            {
                "name": "REDMINE_URL",
                "description": "Endereço do Redmine",
                "required": True,
                "secret": False,
                "env": "REDMINE_URL",
                "file": ".claude/apis/redmine.env",
                "key": "REDMINE_URL",
            },
            {
                "name": "REDMINE_API_KEY",
                "description": "Sua chave de API do Redmine",
                "required": True,
                "secret": True,
                "env": "REDMINE_API_KEY",
            },
        ],
    }
    assert registry.store[(NAME, "1")]["spec"] == registry.store[(NAME, "latest")]["spec"]
    calls = _registry_calls(log)
    assert [(call[0], call[3]) for call in calls] == [
        ("tags", None),
        ("apply", "1"),
        ("put", "1"),
        ("apply", "latest"),
        ("put", "latest"),
    ]
    assert all(call[1] == "app" for call in calls if call[0] in ("tags", "put"))
    assert all(call[4] == AUTH for call in calls)


@pytest.mark.asyncio
async def test_app01_different_wheel_creates_next_version_and_keeps_old_artifact():
    catalog, registry, _ = _catalog()
    first = _app()
    second = _app(WHEEL_V2)
    await catalog.publish(first, "pessoa-1")

    result = await catalog.publish(second, "pessoa-2")

    assert (result.version, result.created, result.sha256) == ("2", True, SHA_V2)
    latest = registry.store[(NAME, "latest")]["metadata"]["annotations"]
    assert (latest["sharemem.io/versao"], latest["sharemem.io/autor"], latest["sharemem.io/sha256"]) == (
        "2",
        "pessoa-2",
        SHA_V2,
    )
    assert registry.artifacts[(NAME, "latest")] == registry.artifacts[(NAME, "2")] == build_app_archive(second)[0]
    assert registry.artifacts[(NAME, "1")] == build_app_archive(first)[0]
    assert registry.store[(NAME, "1")]["metadata"]["annotations"]["sharemem.io/sha256"] == SHA_V1


@pytest.mark.asyncio
async def test_app01_same_wheel_returns_current_version_without_apply_or_put():
    catalog, _, log = _catalog()
    await catalog.publish(_app(), "pessoa-1")
    await catalog.publish(_app(WHEEL_V2), "pessoa-1")
    log.clear()

    result = await catalog.publish(_app(WHEEL_V2), "pessoa-3")

    assert result.model_dump() == {"name": NAME, "version": "2", "sha256": SHA_V2, "created": False}
    assert [call[0] for call in _registry_calls(log)] == ["tags"]


@pytest.mark.asyncio
async def test_app01_next_version_is_highest_numeric_tag_plus_one():
    catalog, registry, _ = _catalog()
    await catalog.publish(_app(), "pessoa-1")
    ninth = copy.deepcopy(registry.store[(NAME, "1")])
    ninth["metadata"]["tag"] = "9"
    registry.store[(NAME, "9")] = ninth

    result = await catalog.publish(_app(WHEEL_V2), "pessoa-1")

    assert result.version == "10"


@pytest.mark.asyncio
async def test_app01_postgresql_takes_lock_before_reading_tags_and_releases_after_last_put():
    catalog, _, log = _catalog(dialect="postgresql")

    await catalog.publish(_app(), "pessoa-1")

    kinds = [entry[0] for entry in log]
    _, sql, params = log[kinds.index("execute")]
    assert "pg_advisory_xact_lock(hashtext(:key))" in sql
    assert params == {"key": f"app_publish:{NAME}"}
    assert kinds.index("execute") < kinds.index("tags") < kinds.index("apply")
    last_put = len(kinds) - 1 - kinds[::-1].index("put")
    assert kinds.count("end_transaction") == 1
    assert last_put < kinds.index("end_transaction")


@pytest.mark.asyncio
async def test_app01_lock_wait_runs_off_the_event_loop_thread():
    catalog, _, _ = _catalog(dialect="postgresql")
    threads = []
    catalog.db.execute.side_effect = lambda stmt, params=None: threads.append(threading.get_ident())

    await catalog.publish(_app(), "pessoa-1")

    assert threads and threads[0] != threading.get_ident()


@pytest.mark.asyncio
async def test_app01_sqlite_does_not_take_lock():
    catalog, _, log = _catalog(dialect="sqlite")

    await catalog.publish(_app(), "pessoa-1")

    assert [entry for entry in log if entry[0] == "execute"] == []


@pytest.mark.asyncio
async def test_app01_rollback_copies_spec_and_artifact_of_old_version_to_latest():
    catalog, registry, _ = _catalog()
    first = _app()
    await catalog.publish(first, "pessoa-1")
    await catalog.publish(_app(WHEEL_V2), "pessoa-1")

    view = await catalog.rollback(NAME, "1", "pessoa-2")

    assert (view.version, view.is_latest, view.sha256) == ("1", True, SHA_V1)
    assert view.card.author == "pessoa-2"
    latest = registry.store[(NAME, "latest")]
    assert latest["spec"] == registry.store[(NAME, "1")]["spec"]
    assert registry.artifacts[(NAME, "latest")] == build_app_archive(first)[0]
    annotations = latest["metadata"]["annotations"]
    assert annotations["sharemem.io/versao"] == "1"
    assert annotations["sharemem.io/autor"] == "pessoa-2"
    assert annotations["sharemem.io/sha256"] == SHA_V1
    assert registry.store[(NAME, "1")]["metadata"]["annotations"]["sharemem.io/autor"] == "pessoa-1"
    assert registry.artifacts[(NAME, "2")] == build_app_archive(_app(WHEEL_V2))[0]
    assert {(NAME, "1"), (NAME, "2"), (NAME, "latest")} == set(registry.store)


@pytest.mark.asyncio
async def test_app01_rollback_applies_latest_before_putting_its_artifact():
    catalog, _, log = _catalog()
    await catalog.publish(_app(), "pessoa-1")
    await catalog.publish(_app(WHEEL_V2), "pessoa-1")
    log.clear()

    await catalog.rollback(NAME, "1", "pessoa-2")

    assert [(call[0], call[3]) for call in _registry_calls(log)] == [
        ("get", "1"),
        ("artifact", "1"),
        ("apply", "latest"),
        ("put", "latest"),
        ("tags", None),
    ]


@pytest.mark.asyncio
async def test_app01_republishing_rolled_back_wheel_keeps_latest_version():
    catalog, _, log = _catalog()
    await catalog.publish(_app(), "pessoa-1")
    await catalog.publish(_app(WHEEL_V2), "pessoa-1")
    await catalog.rollback(NAME, "1", "pessoa-2")
    log.clear()

    result = await catalog.publish(_app(), "pessoa-3")

    assert (result.version, result.created) == ("1", False)
    assert [call[0] for call in _registry_calls(log)] == ["tags"]


@pytest.mark.asyncio
async def test_app01_rollback_takes_publish_lock_before_reading_version():
    catalog, _, log = _catalog(dialect="postgresql")
    await catalog.publish(_app(), "pessoa-1")
    await catalog.publish(_app(WHEEL_V2), "pessoa-1")
    log.clear()

    await catalog.rollback(NAME, "1", "pessoa-2")

    kinds = [entry[0] for entry in log]
    _, sql, params = log[kinds.index("execute")]
    assert "pg_advisory_xact_lock(hashtext(:key))" in sql
    assert params == {"key": f"app_publish:{NAME}"}
    assert kinds.index("execute") < kinds.index("get") < kinds.index("apply")
    assert kinds.index("put") < kinds.index("end_transaction")


@pytest.mark.asyncio
async def test_app01_rollback_to_missing_version_is_reported():
    catalog, _, _ = _catalog()
    await catalog.publish(_app(), "pessoa-1")

    with pytest.raises(AppRuleError) as exc:
        await catalog.rollback(NAME, "9", "pessoa-2")

    assert str(exc.value) == f"Aplicativo não encontrado: {NAME}@9"
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_app01_get_without_version_returns_latest_view():
    catalog, registry, log = _catalog()
    await catalog.publish(_app(), "pessoa-1")
    await catalog.publish(_app(WHEEL_V2), "pessoa-2")
    log.clear()

    view = await catalog.get(NAME)

    assert (view.name, view.version, view.is_latest) == (NAME, "2", True)
    assert (view.sha256, view.wheel_name, view.python_requires) == (SHA_V2, WHEEL_NAME, ">=3.11")
    card = view.card
    assert card.title == "Controle de horas"
    assert card.description == "Controle de horas local com lançamento no Redmine."
    assert card.usage == "Inicie com `horas-app` e confira com `horas-app verificar`."
    assert card.requirements == ["Python 3.11 ou mais"]
    assert card.effects == ["grava-arquivos", "acessa-rede"]
    assert card.area == "redmine"
    assert card.tags == ["horas"]
    assert card.author == "pessoa-2"
    assert card.published_at == registry.store[(NAME, "2")]["metadata"]["annotations"]["sharemem.io/publicado-em"]
    assert view.commands.model_dump() == {"start": "horas-app", "verify": "horas-app verificar"}
    assert [setting.model_dump() for setting in view.settings] == [
        {
            "name": "REDMINE_URL",
            "description": "Endereço do Redmine",
            "required": True,
            "secret": False,
            "env": "REDMINE_URL",
            "file": ".claude/apis/redmine.env",
            "key": "REDMINE_URL",
        },
        {
            "name": "REDMINE_API_KEY",
            "description": "Sua chave de API do Redmine",
            "required": True,
            "secret": True,
            "env": "REDMINE_API_KEY",
            "file": None,
            "key": None,
        },
    ]
    assert [(call[0], call[3]) for call in _registry_calls(log)] == [("get", "latest"), ("tags", None)]


@pytest.mark.asyncio
async def test_app01_get_fixed_version_returns_that_version_not_latest():
    catalog, _, _ = _catalog()
    await catalog.publish(_app(), "pessoa-1")
    await catalog.publish(_app(WHEEL_V2), "pessoa-2")

    view = await catalog.get(NAME, "1")

    assert (view.version, view.is_latest, view.sha256, view.card.author) == ("1", False, SHA_V1, "pessoa-1")


@pytest.mark.asyncio
async def test_app01_get_missing_version_is_reported_with_name_and_version():
    catalog, _, _ = _catalog()

    with pytest.raises(AppRuleError) as exc:
        await catalog.get("nao-existe", "9")
    with pytest.raises(AppRuleError) as latest_exc:
        await catalog.get("nao-existe")

    assert str(exc.value) == "Aplicativo não encontrado: nao-existe@9"
    assert exc.value.status_code == 404
    assert str(latest_exc.value) == "Aplicativo não encontrado: nao-existe@latest"


@pytest.mark.asyncio
async def test_app01_versions_list_newest_first_with_author_date_and_latest_mark():
    catalog, registry, _ = _catalog()
    await catalog.publish(_app(), "pessoa-1")
    await catalog.publish(_app(WHEEL_V2), "pessoa-2")
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
async def test_app01_read_artifact_returns_stored_bytes_of_the_version():
    catalog, _, log = _catalog()
    first = _app()
    await catalog.publish(first, "pessoa-1")
    await catalog.publish(_app(WHEEL_V2), "pessoa-1")
    log.clear()

    archive = await catalog.read_artifact(NAME, "1")

    assert archive == build_app_archive(first)[0]
    assert _registry_calls(log) == [("artifact", "app", NAME, "1", AUTH)]


@pytest.mark.asyncio
async def test_app01_read_artifact_of_missing_version_is_reported():
    catalog, _, _ = _catalog()

    with pytest.raises(AppRuleError) as exc:
        await catalog.read_artifact(NAME, "9")

    assert str(exc.value) == f"Aplicativo não encontrado: {NAME}@9"
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_app01_connection_failure_becomes_store_unavailable():
    catalog, registry, _ = _catalog()
    registry.error = AgentRegistryError(502, "AgentRegistry indisponível")

    for call in (
        catalog.get(NAME),
        catalog.publish(_app(), "pessoa-1"),
        catalog.rollback(NAME, "1", "pessoa-1"),
        catalog.read_artifact(NAME, "1"),
    ):
        with pytest.raises(AppRuleError) as exc:
            await call
        assert str(exc.value) == "Store indisponível; tente de novo em instantes"
        assert exc.value.status_code == 502


@pytest.mark.asyncio
async def test_app01_rate_limit_reports_wait_seconds():
    catalog, registry, _ = _catalog()
    registry.error = AgentRegistryRateLimited("7")

    for call in (catalog.get(NAME), catalog.publish(_app(), "pessoa-1")):
        with pytest.raises(AppRuleError) as exc:
            await call
        assert str(exc.value) == "Limite da Store atingido; tente em 7 s"
        assert exc.value.status_code == 429


@pytest.mark.asyncio
async def test_app01_publish_and_rollback_are_logged(caplog):
    catalog, _, _ = _catalog()
    app = _app()
    size = len(build_app_archive(app)[0])
    caplog.set_level(logging.INFO)

    await catalog.publish(app, "pessoa-1")
    await catalog.publish(app, "pessoa-1")
    await catalog.rollback(NAME, "1", "pessoa-2")

    messages = [record.getMessage() for record in caplog.records]
    assert f"app_publish name={NAME} version=1 created=True author=pessoa-1 size={size}" in messages
    assert f"app_publish name={NAME} version=1 created=False author=pessoa-1 size={size}" in messages
    assert f"app_rollback name={NAME} version=1 author=pessoa-2" in messages


@pytest.mark.asyncio
async def test_app01_minimal_card_omits_empty_optionals_from_spec():
    catalog, registry, _ = _catalog()

    await catalog.publish(_app(title=None, requirements=[], tags=[], settings=[]), "pessoa-1")

    for tag in ("1", "latest"):
        assert not {"title", "requirements", "tags", "settings"} & set(registry.store[(NAME, tag)]["spec"])


@pytest.mark.asyncio
async def test_app01_latest_without_version_annotation_is_not_latest():
    catalog, registry, _ = _catalog()
    await catalog.publish(_app(), "pessoa-1")
    for tag in ("1", "latest"):
        registry.store[(NAME, tag)]["metadata"]["annotations"].pop("sharemem.io/versao")

    view = await catalog.get(NAME)

    assert view.is_latest is False


@pytest.mark.asyncio
async def test_app01_fixed_version_view_marks_real_latest_in_versions():
    catalog, _, _ = _catalog()
    await catalog.publish(_app(), "pessoa-1")
    await catalog.publish(_app(WHEEL_V2), "pessoa-2")

    view = await catalog.get(NAME, "1")

    assert [(version.version, version.is_latest) for version in view.versions] == [("2", True), ("1", False)]


@pytest.mark.asyncio
async def test_app01_publish_not_found_reports_latest():
    catalog, registry, _ = _catalog()
    registry.error = AgentRegistryResourceNotFound()

    with pytest.raises(AppRuleError) as exc:
        await catalog.publish(_app(), "pessoa-1")

    assert str(exc.value) == f"Aplicativo não encontrado: {NAME}@latest"
    assert exc.value.status_code == 404
