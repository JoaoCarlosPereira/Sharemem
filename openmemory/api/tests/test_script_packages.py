import hashlib

import pytest

from app.services.script_packages import (
    ScriptInput,
    ScriptRuleError,
    content_sha256,
    effect_warning,
    find_credential_line,
    to_registry_resource,
)

MAX_BYTES = 256 * 1024


def _payload(**overrides):
    payload = {
        "name": "gcenfe02-xml",
        "file_name": "gcenfe02_xml.py",
        "title": "Decodificador GCENFE02",
        "description": "Decodifica a coluna GCENFE02.XML",
        "usage": "python gcenfe02_xml.py <arquivo>",
        "language": "python",
        "requirements": ["python>=3.8"],
        "effects": ["somente-leitura"],
        "area": "sysmo-s1",
        "tags": ["nfe", "xml"],
        "content": "print('ok')\n",
    }
    payload.update(overrides)
    return payload


def _rule_message(**overrides):
    with pytest.raises(ScriptRuleError) as exc:
        ScriptInput(**_payload(**overrides))
    return str(exc.value)


@pytest.mark.parametrize(
    "field,empty",
    [
        ("description", ""),
        ("usage", "  "),
        ("language", ""),
        ("effects", []),
        ("area", ""),
    ],
)
def test_scr02_empty_required_field_is_refused(field, empty):
    assert _rule_message(**{field: empty}) == f"Campo obrigatório ausente: {field}"


def test_scr02_missing_required_field_is_refused():
    payload = _payload()
    del payload["usage"]
    with pytest.raises(ScriptRuleError) as exc:
        ScriptInput(**payload)
    assert str(exc.value) == "Campo obrigatório ausente: usage"


def test_scr03_extension_outside_list_is_refused():
    assert _rule_message(file_name="x.exe") == "Extensão não aceita: .exe"


@pytest.mark.parametrize(
    "file_name", ["x.py", "x.sql", "x.ps1", "x.sh", "x.bat", "x.cmd", "x.js", "x.mjs"]
)
def test_scr03_accepted_extensions_pass(file_name):
    assert ScriptInput(**_payload(file_name=file_name)).file_name == file_name


def test_scr04_content_above_256kb_is_refused():
    assert _rule_message(content="a" * (MAX_BYTES + 1)) == "Script acima de 256 KB"


def test_scr04_content_with_exactly_256kb_passes():
    assert len(ScriptInput(**_payload(content="a" * MAX_BYTES)).content) == MAX_BYTES


def test_scr04_limit_counts_utf8_bytes_not_characters():
    content = "ç" * (MAX_BYTES // 2) + "a"
    assert _rule_message(content=content) == "Script acima de 256 KB"


def test_scr05_omtk_token_reports_line_number():
    content = "import os\nprint('x')\ntoken = 'omtk_abc123'\n"
    assert _rule_message(content=content) == "Script contém credencial na linha 3"


@pytest.mark.parametrize(
    "line",
    [
        "PGPASSWORD=segredo psql -h host",
        'export PGPASSWORD="segredo"',
        'conn = connect(password="x")',
        "dsn = 'host=db password=segredo dbname=x'",
    ],
)
def test_scr05_literal_password_is_found(line):
    assert find_credential_line(f"echo inicio\n{line}\n") == 2


@pytest.mark.parametrize(
    "line",
    [
        "PGPASSWORD=$PGPASS psql -h host",
        'export PGPASSWORD="$PGPASS"',
        "set PGPASSWORD=%PGPASS%",
        "conn = connect(password=senha)",
        "PGPASSWORD=",
        'if password == "x":',
        "atomtk_foo = 1",
    ],
)
def test_scr05_variable_reference_is_not_credential(line):
    assert find_credential_line(f"echo inicio\n{line}\n") is None


def test_scr05_publication_with_pgpassword_literal_is_refused():
    content = "set -e\nPGPASSWORD=x psql\n"
    assert _rule_message(content=content) == "Script contém credencial na linha 2"


@pytest.mark.parametrize("name", ["A", "a", "gcenfe02_xml", "a" * 65, "-ab", "ab-", "a--b"])
def test_scr06_invalid_name_is_refused(name):
    assert _rule_message(name=name) == "Nome inválido"


@pytest.mark.parametrize("name", ["gcenfe02-xml", "ab", "a" * 64])
def test_scr06_valid_name_passes(name):
    assert ScriptInput(**_payload(name=name)).name == name


@pytest.mark.parametrize("effect", ["altera-banco", "grava-arquivos"])
def test_scr13_sensitive_effect_has_warning(effect):
    assert (
        effect_warning(["somente-leitura", effect])
        == f"Este script {effect}; confirme com o usuário antes de rodar"
    )


@pytest.mark.parametrize("effects", [["somente-leitura"], ["acessa-rede"], []])
def test_scr13_no_warning_without_sensitive_effect(effects):
    assert effect_warning(effects) is None


def test_scr13_unknown_effect_is_refused_by_model():
    with pytest.raises(ValueError):
        ScriptInput(**_payload(effects=["apaga-tudo"]))


def test_scr14_sha256_is_over_utf8_bytes_without_normalizing_newlines():
    crlf = "ação\r\nlinha\r\n"
    lf = "ação\nlinha\n"
    assert content_sha256(crlf) == hashlib.sha256(crlf.encode("utf-8")).hexdigest()
    assert content_sha256(crlf) != content_sha256(lf)


def test_scr14_content_is_kept_byte_for_byte():
    content = "ação\r\nlinha\r\n"
    script = ScriptInput(**_payload(content=content))
    assert script.content == content


def test_scr27_invalid_utf8_bytes_are_refused():
    assert _rule_message(content=b"print(1)\n\xff\xfe") == "Script precisa ser texto UTF-8"


def test_scr27_lone_surrogate_text_is_refused():
    assert _rule_message(content="print(1)\udcff") == "Script precisa ser texto UTF-8"


def test_scr27_valid_utf8_bytes_are_decoded():
    script = ScriptInput(**_payload(content="ação\n".encode("utf-8")))
    assert script.content == "ação\n"


def test_scr28_description_above_4000_chars_is_refused():
    assert _rule_message(description="d" * 4001) == "Descrição acima de 4.000 caracteres"


def test_scr28_description_with_4000_chars_passes():
    assert len(ScriptInput(**_payload(description="d" * 4000)).description) == 4000


def test_to_registry_resource_builds_script_kind_with_annotations():
    script = ScriptInput(**_payload())
    annotations = {"sharemem.io/versao": "1", "sharemem.io/autor": "pessoa"}

    resource = to_registry_resource(script, "1", annotations)

    assert resource == {
        "apiVersion": "ar.dev/v1alpha1",
        "kind": "Script",
        "metadata": {"name": "gcenfe02-xml", "tag": "1", "annotations": annotations},
        "spec": {
            "title": "Decodificador GCENFE02",
            "description": "Decodifica a coluna GCENFE02.XML",
            "usage": "python gcenfe02_xml.py <arquivo>",
            "fileName": "gcenfe02_xml.py",
            "language": "python",
            "requirements": ["python>=3.8"],
            "effects": ["somente-leitura"],
            "area": "sysmo-s1",
            "tags": ["nfe", "xml"],
            "content": "print('ok')\n",
        },
    }


def test_to_registry_resource_omits_empty_optional_fields():
    script = ScriptInput(**_payload(title=None, requirements=[], tags=[]))

    spec = to_registry_resource(script, "latest", {})["spec"]

    assert "title" not in spec
    assert "requirements" not in spec
    assert "tags" not in spec


def test_to_registry_resource_keeps_required_content_key_even_when_empty():
    script = ScriptInput(**_payload(content=""))

    assert to_registry_resource(script, "1", {})["spec"]["content"] == ""
