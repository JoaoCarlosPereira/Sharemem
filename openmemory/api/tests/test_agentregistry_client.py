import httpx
import pytest

from app.utils.agentregistry import (
    KIND_TO_REGISTRY_COLLECTION,
    REGISTRY_KIND_TO_CATALOG_KIND,
    AgentRegistryHttpClient,
    AgentRegistryRateLimited,
    _json_response,
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


def test_scr26_http_429_keeps_status_and_retry_after():
    response = httpx.Response(429, headers={"Retry-After": "7"})

    with pytest.raises(AgentRegistryRateLimited) as exc:
        _json_response(response)

    assert exc.value.status_code == 429
    assert exc.value.retry_after == "7"


def test_scr07_terms_also_match_script_area_and_tags():
    assert resource_matches_query(_script_resource(), "sysmo-s1 nfe") is True


def _app_resource():
    return {
        "apiVersion": "ar.dev/v1alpha1",
        "kind": "LocalApp",
        "metadata": {
            "namespace": "default",
            "name": "horas-app",
            "tag": "latest",
            "annotations": {"sharemem.io/autor": "pessoa-1", "sharemem.io/versao": "3"},
        },
        "spec": {
            "title": "Controle de horas",
            "description": "Controle de horas local com lançamento no Redmine",
            "usage": "horas-app",
            "requirements": ["Python 3.11 ou mais"],
            "effects": ["grava-arquivos", "acessa-rede"],
            "area": "redmine",
            "tags": ["horas"],
            "wheel": "horas_app-0.1.0-py3-none-any.whl",
            "pythonRequires": ">=3.11",
            "commands": {"start": "horas-app", "verify": "horas-app verificar"},
            "settings": [
                {"name": "REDMINE_URL", "description": "Endereço", "required": True, "secret": False, "env": "REDMINE_URL"},
                {"name": "REDMINE_API_KEY", "description": "Chave", "required": True, "secret": True, "env": "REDMINE_API_KEY"},
            ],
        },
    }


def test_app03_app_kind_maps_to_localapps_collection():
    assert KIND_TO_REGISTRY_COLLECTION["app"] == "localapps"
    assert REGISTRY_KIND_TO_CATALOG_KIND["LocalApp"] == "app"


@pytest.mark.asyncio
async def test_app03_list_resources_of_apps_uses_localapps_with_latest_only():
    client = RecordingClient()

    await client.list_resources(kind="app", latest_only=True)

    method, path, params = client.requests[0]
    assert (method, path) == ("GET", "/v0/localapps")
    assert params["latestOnly"] == "true"


def test_app03_app_summary_carries_card_and_settings_without_values():
    summary = summarize_resource(_app_resource())

    assert summary["kind"] == "app"
    assert summary["title"] == "Controle de horas"
    assert summary["usage"] == "horas-app"
    assert summary["requirements"] == ["Python 3.11 ou mais"]
    assert summary["effects"] == ["grava-arquivos", "acessa-rede"]
    assert summary["area"] == "redmine"
    assert summary["tags"] == ["horas"]
    assert summary["author"] == "pessoa-1"
    assert summary["version"] == "3"
    assert summary["settings"] == [
        {"name": "REDMINE_URL", "required": True, "secret": False},
        {"name": "REDMINE_API_KEY", "required": True, "secret": True},
    ]


def test_app03_app_matches_query_by_area_and_tags():
    assert resource_matches_query(_app_resource(), "horas redmine") is True
    assert resource_matches_query(_app_resource(), "horas boleto") is False


def test_app03_script_and_skill_summaries_do_not_get_app_fields():
    assert "settings" not in summarize_resource(_script_resource())
    skill = summarize_resource({"kind": "Skill", "metadata": {"name": "x"}, "spec": {}})
    assert set(skill) == {"kind", "registry_kind", "namespace", "name", "tag", "title", "description", "labels", "annotations"}


class ArtifactClient(AgentRegistryHttpClient):
    def __init__(self):
        super().__init__(base_url="http://registry.test")
        self.calls = []

    async def _request_raw(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs["headers"]))
        return {}


@pytest.mark.asyncio
async def test_app01_put_package_artifact_of_app_sends_localapp_media_type():
    client = ArtifactClient()

    await client.put_package_artifact(kind="app", name="horas-app", tag="1", archive=b"pacote")

    method, path, headers = client.calls[0]
    assert (method, path) == ("PUT", "/v0/localapps/horas-app/1/artifact")
    assert headers["Content-Type"] == "application/vnd.agentregistry.localapp.v1.tar+gzip"
