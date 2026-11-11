import pytest

from app.services.catalog_versions import PUBLISHED_KEY
from app.services.script_packages import ScriptRuleError
from app.utils.agentregistry import AgentRegistryError, AgentRegistryRateLimited
from tests.test_script_catalog import AUTH, NAME, _catalog, _script


@pytest.mark.asyncio
async def test_rate_limit_keeps_status_429():
    catalog, registry, _ = _catalog()
    registry.error = AgentRegistryRateLimited("7")

    with pytest.raises(ScriptRuleError) as error:
        await catalog.get(NAME)

    assert error.value.status_code == 429


@pytest.mark.asyncio
async def test_other_registry_error_passes_detail_and_status():
    catalog, registry, _ = _catalog()
    registry.error = AgentRegistryError(503, "boom")

    with pytest.raises(ScriptRuleError) as error:
        await catalog.get(NAME)

    assert str(error.value) == "boom"
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_reads_use_catalog_kind_and_auth_headers():
    catalog, _, log = _catalog()
    await catalog.publish(_script(), "pessoa-1")
    log.clear()

    await catalog.get(NAME)

    reads = [entry for entry in log if entry[0] in ("get", "tags")]
    assert [entry[0] for entry in reads] == ["get", "tags"]
    assert all(entry[1] == "script" and entry[4] == AUTH for entry in reads)


@pytest.mark.asyncio
async def test_rollback_copy_keeps_kind_and_refreshes_published_at():
    catalog, registry, _ = _catalog()
    await catalog.publish(_script(), "pessoa-1")
    first_published = registry.store[(NAME, "1")]["metadata"]["annotations"][PUBLISHED_KEY]
    await catalog.publish(_script(content="print('v2')\n"), "pessoa-2")

    await catalog.rollback(NAME, "1", "pessoa-3")

    latest = registry.store[(NAME, "latest")]
    assert latest["kind"] == "Script"
    assert latest["apiVersion"] == "ar.dev/v1alpha1"
    assert latest["metadata"]["annotations"][PUBLISHED_KEY] != first_published


@pytest.mark.asyncio
async def test_versions_are_sorted_as_numbers():
    catalog, _, _ = _catalog()
    for number in range(1, 11):
        await catalog.publish(_script(content=f"print({number})\n"), "pessoa-1")

    view = await catalog.get(NAME)

    assert [item.version for item in view.versions] == [str(number) for number in range(10, 0, -1)]


@pytest.mark.asyncio
async def test_tags_without_items_publish_first_version():
    catalog, registry, _ = _catalog()

    async def no_items(**kwargs):
        return {"items": None}

    registry.list_tags = no_items

    result = await catalog.publish(_script(), "pessoa-1")

    assert result.version == "1"
