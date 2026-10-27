import pytest

from app.utils.agentregistry import (
    KIND_TO_REGISTRY_COLLECTION,
    REGISTRY_KIND_TO_CATALOG_KIND,
    AgentRegistryHttpClient,
    resource_matches_query,
    summarize_resource,
)


def _script_resource(description="Decodifica a coluna GCENFE02.XML"):
    return {
        "apiVersion": "ar.dev/v1alpha1",
        "kind": "Script",
        "metadata": {
            "namespace": "default",
            "name": "gcenfe02-xml",
            "tag": "latest",
            "annotations": {"sharemem.io/autor": "pessoa-1", "sharemem.io/versao": "2"},
        },
        "spec": {
            "description": description,
            "usage": "python gcenfe02_xml.py <arquivo>",
            "fileName": "gcenfe02_xml.py",
            "language": "python",
            "effects": ["somente-leitura"],
            "area": "sysmo-s1",
            "tags": ["nfe", "xml"],
            "content": "print(1)\n",
        },
    }


class RecordingClient(AgentRegistryHttpClient):
    def __init__(self):
        super().__init__(base_url="http://registry.test")
        self.requests = []

    async def _request(self, method, path, **kwargs):
        self.requests.append((method, path, kwargs.get("params")))
        return {"items": []}


def test_script_kind_maps_to_scripts_collection():
    assert KIND_TO_REGISTRY_COLLECTION["script"] == "scripts"
    assert REGISTRY_KIND_TO_CATALOG_KIND["Script"] == "script"


@pytest.mark.asyncio
async def test_list_resources_latest_only_sends_query_flag():
    client = RecordingClient()

    await client.list_resources(kind="script", latest_only=True)

    method, path, params = client.requests[0]
    assert (method, path) == ("GET", "/v0/scripts")
    assert params["latestOnly"] == "true"


@pytest.mark.asyncio
async def test_list_resources_without_latest_only_keeps_previous_query():
    client = RecordingClient()

    await client.list_resources(kind="skill")

    assert "latestOnly" not in client.requests[0][2]


@pytest.mark.asyncio
async def test_list_tags_calls_tags_route_of_the_collection():
    client = RecordingClient()

    await client.list_tags(kind="script", name="gcenfe02-xml")

    assert client.requests[0] == ("GET", "/v0/scripts/gcenfe02-xml/tags", {"namespace": "default"})


def test_scr07_script_summary_carries_card_author_and_version():
    summary = summarize_resource(_script_resource())

    assert summary["kind"] == "script"
    assert summary["name"] == "gcenfe02-xml"
    assert summary["description"] == "Decodifica a coluna GCENFE02.XML"
    assert summary["language"] == "python"
    assert summary["effects"] == ["somente-leitura"]
    assert summary["area"] == "sysmo-s1"
    assert summary["tags"] == ["nfe", "xml"]
    assert summary["author"] == "pessoa-1"
    assert summary["version"] == "2"


@pytest.mark.parametrize("query", ["XML GCENFE02", "gcenfe02 xml", "COLUNA decodifica"])
def test_scr07_all_terms_match_in_any_order_ignoring_case(query):
    assert resource_matches_query(_script_resource(), query) is True


def test_scr07_phrase_that_matched_before_still_matches():
    assert resource_matches_query(_script_resource(), "decodifica a coluna") is True


def test_scr07_query_with_a_missing_term_does_not_match():
    assert resource_matches_query(_script_resource(), "gcenfe02 boleto") is False


def test_scr07_terms_also_match_script_area_and_tags():
    assert resource_matches_query(_script_resource(), "sysmo-s1 nfe") is True
