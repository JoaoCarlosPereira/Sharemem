import gzip
import hashlib
import io
import os
import tarfile
import zipfile

import pytest

from app.services.app_packages import AppInput, AppRuleError, build_app_archive, render_app_md

WHEEL_NAME = "horas_app-0.1.0-py3-none-any.whl"
METADATA = "horas_app-0.1.0.dist-info/METADATA"


def _wheel(
    name: str = "horas-app",
    version: str | None = "0.1.0",
    requires: str | None = ">=3.11",
    metadata_path: str | None = METADATA,
    extra: dict[str, bytes] | None = None,
    raw_metadata: bytes | None = None,
) -> bytes:
    lines = ["Metadata-Version: 2.4", f"Name: {name}"]
    if version:
        lines.append(f"Version: {version}")
    if requires:
        lines.append(f"Requires-Python: {requires}")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as wheel:
        wheel.writestr("horas_app/__init__.py", "")
        if metadata_path:
            wheel.writestr(metadata_path, raw_metadata if raw_metadata is not None else "\n".join(lines) + "\n")
        for path, content in (extra or {}).items():
            wheel.writestr(path, content)
    return buffer.getvalue()


def _setting(name="REDMINE_URL", **changes) -> dict:
    setting = {"name": name, "description": "Endereço do Redmine", "required": True, "secret": False, "env": name}
    setting.update(changes)
    return setting


def _card(**changes) -> dict:
    card = {
        "name": "horas-app",
        "title": "Controle de horas",
        "description": "Controle de horas local com lançamento no Redmine.",
        "usage": "Inicie com `horas-app` e confira com `horas-app verificar`.",
        "requirements": ["Python 3.11 ou mais"],
        "effects": ["grava-arquivos", "acessa-rede"],
        "area": "redmine",
        "tags": ["horas"],
        "commands": {"start": "horas-app", "verify": "horas-app verificar"},
        "settings": [
            _setting(file=".claude/apis/redmine.env", key="REDMINE_URL"),
            _setting("REDMINE_API_KEY", description="Sua chave de API do Redmine", secret=True),
        ],
    }
    card.update(changes)
    return card


def _app(card: dict | None = None, wheel: bytes | None = None, wheel_name: str = WHEEL_NAME) -> AppInput:
    return AppInput.from_upload(card or _card(), wheel_name, _wheel() if wheel is None else wheel)


def _rejects(match: str, status: int = 422, **kwargs):
    with pytest.raises(AppRuleError, match=match) as error:
        _app(**kwargs)
    assert error.value.status_code == status


def test_app01_valid_card_and_wheel_reads_metadata_from_wheel():
    app = _app()
    assert app.name == "horas-app"
    assert app.wheel_name == WHEEL_NAME
    assert app.python_requires == ">=3.11"
    assert app.title == "Controle de horas"
    assert app.requirements == ["Python 3.11 ou mais"]
    assert app.tags == ["horas"]
    assert [setting.name for setting in app.settings] == ["REDMINE_URL", "REDMINE_API_KEY"]
    assert [setting.secret for setting in app.settings] == [False, True]


def test_app01_optional_fields_default_to_empty():
    card = _card()
    for field in ("title", "requirements", "tags", "settings"):
        card.pop(field)
    app = _app(card)
    assert (app.title, app.requirements, app.tags, app.settings) == (None, [], [], [])


def test_app01_python_requires_comes_from_wheel_not_from_card():
    app = _app(_card(python_requires=">=2.7", wheel_name="outro.whl"))
    assert app.python_requires == ">=3.11"
    assert app.wheel_name == WHEEL_NAME


@pytest.mark.parametrize("field, empty", [("description", ""), ("description", "   "), ("usage", ""), ("effects", []), ("area", "")])
def test_app01_missing_required_card_field_is_rejected(field, empty):
    _rejects(f"^Campo obrigatório ausente: {field}$", card=_card(**{field: empty}))


@pytest.mark.parametrize("command", ["start", "verify"])
@pytest.mark.parametrize("empty", ["", "   "])
def test_app01_missing_command_is_rejected(command, empty):
    commands = {"start": "horas-app", "verify": "horas-app verificar", command: empty}
    _rejects(f"^Campo obrigatório ausente: commands.{command}$", card=_card(commands=commands))


@pytest.mark.parametrize("commands", [None, "horas-app", ["horas-app"]])
def test_app01_commands_not_an_object_is_rejected(commands):
    _rejects("^Campo obrigatório ausente: commands.start$", card=_card(commands=commands))


@pytest.mark.parametrize("name", ["Horas", "horas_app", "a", "x" * 65, "-horas", "horas-app\n"])
def test_app01_invalid_name_is_rejected(name):
    _rejects("^Nome inválido$", card=_card(name=name))


def test_app01_description_above_4000_chars_is_rejected():
    _rejects("^Descrição acima de 4.000 caracteres$", card=_card(description="a" * 4001))


def test_app01_description_with_4000_chars_is_accepted():
    assert len(_app(_card(description="a" * 4000)).description) == 4000


def test_app01_unknown_effect_is_rejected():
    with pytest.raises(Exception, match="effects"):
        _app(_card(effects=["xyz"]))


@pytest.mark.parametrize(
    "wheel_name",
    ["horas_app-0.1.0.zip", "pasta/horas_app-0.1.0-py3-none-any.whl", "outro.whl", "..whl", f"{WHEEL_NAME}\n", "horas_app-0.1.0-py3-none-any.whl.zip"],
)
def test_app01_wheel_file_name_outside_convention_is_rejected(wheel_name):
    _rejects("precisa ser um .whl sem caminho", wheel_name=wheel_name)


@pytest.mark.parametrize(
    "wheel, match",
    [
        (b"isto nao e um zip", "^Arquivo .whl inválido$"),
        (_wheel(metadata_path=None), "^Arquivo .whl sem METADATA$"),
        (_wheel(metadata_path="horas_app/METADATA"), "^Arquivo .whl sem METADATA$"),
        (_wheel(metadata_path="vendor/x-1.0.dist-info/METADATA"), "^Arquivo .whl sem METADATA$"),
        (_wheel(extra={"outro-1.0.dist-info/METADATA": b"Name: outro\n"}), "^Arquivo .whl sem METADATA$"),
        (_wheel(raw_metadata=b"Name: horas-app\nVersion: \xff\xfe\n"), "^Arquivo .whl inválido$"),
        (_wheel(version=None), "^METADATA do .whl sem Version$"),
        (_wheel(requires=None), "^METADATA do .whl sem Requires-Python$"),
        (_wheel(raw_metadata=b"Name: horas-app\n" + b"x" * (1 << 20)), "^METADATA do .whl acima de 1 MiB$"),
    ],
    ids=["zip-corrompido", "sem-metadata", "metadata-fora-do-dist-info", "metadata-aninhado", "dois-metadata", "utf8-invalido", "sem-version", "sem-requires-python", "metadata-gigante"],
)
def test_app01_invalid_wheel_is_rejected(wheel, match):
    _rejects(match, wheel=wheel)


def test_app01_wheel_name_must_match_app_name():
    _rejects("outro-app", wheel=_wheel(name="outro-app"))


@pytest.mark.parametrize("metadata_name", ["horas_app", "Horas.App", "horas-app", "horas__app", "horas.-app"])
def test_app01_wheel_name_is_normalized(metadata_name):
    assert _app(wheel=_wheel(name=metadata_name)).name == "horas-app"


def _padded_wheel(total: int) -> bytes:
    base = _wheel()
    padding = total - len(_wheel(extra={"pad.bin": b""}))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as wheel:
        with zipfile.ZipFile(io.BytesIO(base)) as source:
            for item in source.infolist():
                wheel.writestr(item, source.read(item))
        wheel.writestr("pad.bin", os.urandom(padding))
    return buffer.getvalue()


def test_app01_wheel_with_exactly_4_mib_is_accepted():
    wheel = _padded_wheel(4 * 1024 * 1024)
    assert len(wheel) == 4 * 1024 * 1024
    assert _app(wheel=wheel).wheel == wheel


def test_app01_wheel_above_4_mib_is_rejected_with_413():
    _rejects("^Arquivo .whl acima de 4 MiB$", status=413, wheel=_wheel() + b"\0" * (4 * 1024 * 1024))


@pytest.mark.parametrize("field", ["start", "verify"])
@pytest.mark.parametrize(
    "command",
    [
        "/usr/bin/horas-app",
        "..\\horas-app",
        "horas-app && rm -rf ~",
        "horas-app | cat",
        "horas-app; ls",
        "C:\\x\\horas.exe",
        "horas-app --verificar",
        "horas-app x=1",
        "horas-app\n",
        ".horas-app",
    ],
)
def test_app01_command_with_path_option_or_shell_operator_is_rejected(field, command):
    commands = {"start": "horas-app", "verify": "horas-app verificar", field: command}
    _rejects(f"commands.{field}", card=_card(commands=commands))


def test_app01_subcommand_with_dots_and_dashes_is_accepted():
    app = _app(_card(commands={"start": "horas-app", "verify": "horas-app verificar-tudo v1.2"}))
    assert app.commands.verify == "horas-app verificar-tudo v1.2"


@pytest.mark.parametrize(
    "setting, match",
    [
        ({"name": "TOKEN", "description": "x", "required": True, "secret": True}, "TOKEN precisa de env"),
        ({"name": "TOKEN", "description": "x", "required": True, "secret": True, "file": ".claude/x.env"}, "file e key vão juntos"),
        (_setting(file=".claude/x.env"), "file e key vão juntos"),
        (_setting(key="REDMINE_URL"), "file e key vão juntos"),
        ({k: v for k, v in _setting().items() if k != "name"}, "^Campo obrigatório ausente: settings.name$"),
        (_setting(name="  "), "^Campo obrigatório ausente: settings.name$"),
        (_setting(required="true"), "^settings.REDMINE_URL.required deve ser true ou false$"),
        (_setting(secret=0), "^settings.REDMINE_URL.secret deve ser true ou false$"),
        (_setting(env="1A"), "env deve ser um identificador"),
        (_setting(file="x.env", key="1A"), "key deve ser um identificador"),
        (_setting(file=".", key="K"), "file deve ser relativo"),
        (_setting(file="a/./b.env", key="K"), "file deve ser relativo"),
        (_setting(file="a\\b.env", key="K"), "file deve ser relativo"),
        (_setting(file="C:/x.env", key="K"), "file deve ser relativo"),
        (_setting(file="", key="K"), "file deve ser relativo"),
        (_setting(description="linha 1\r## injetado"), "uma linha só"),
        (_setting(required=None), "^Campo obrigatório ausente: settings.REDMINE_URL.required$"),
        ({k: v for k, v in _setting().items() if k != "secret"}, "settings.REDMINE_URL.secret"),
        (_setting(name="A`\n## x"), "name deve ser um identificador"),
        (_setting(env="REDMINE URL"), "env deve ser um identificador"),
        (_setting(file="x.env", key="a-b"), "key deve ser um identificador"),
        (_setting(file="/etc/passwd", key="K"), "file deve ser relativo"),
        (_setting(file="../../x.env", key="K"), "file deve ser relativo"),
        (_setting(file="C:\\x.env", key="K"), "file deve ser relativo"),
        (_setting(description="linha 1\n## injetado"), "uma linha só"),
    ],
    ids=["sem-destino", "file-sem-key", "env-e-file-sem-key", "env-e-key-sem-file", "sem-name", "name-vazio", "required-texto", "secret-numero", "env-comeca-com-digito", "key-comeca-com-digito", "file-ponto", "file-ponto-no-meio", "file-barra-invertida", "file-unidade-com-barra", "file-vazio", "descricao-com-cr", "required-ausente", "secret-ausente", "name-injecao", "env-invalido", "key-invalida", "file-absoluto", "file-sobe", "file-unidade", "descricao-multilinha"],
)
def test_app01_invalid_setting_is_rejected(setting, match):
    _rejects(match, card=_card(settings=[setting]))


def test_app01_setting_only_with_file_and_key_is_accepted():
    setting = {"name": "TOKEN", "description": "x", "required": False, "secret": True, "file": ".claude/x.env", "key": "TOKEN"}
    app = _app(_card(settings=[setting]))
    assert app.settings[0].env is None
    assert (app.settings[0].required, app.settings[0].secret) == (False, True)


@pytest.mark.parametrize(
    "card, field",
    [
        (_card(usage="Rode com PGPASSWORD=segredo horas-app"), "usage"),
        (_card(description="Token omtk_abc123 para testes"), "description"),
        (_card(title="omtk_abc123"), "title"),
        (_card(requirements=["Use omtk_abc123"]), "requirements"),
        (_card(settings=[_setting(description="password=segredo")]), "settings.REDMINE_URL"),
        (_card(settings=[_setting(name="omtk_abc", env="X")]), "settings.omtk_abc"),
    ],
    ids=["usage", "description", "title", "requirements", "setting-descricao", "setting-nome"],
)
def test_app01_credential_in_card_is_rejected(card, field):
    _rejects(f"^Ficha contém credencial em {field}$", card=card)


def _members(archive: bytes) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(archive))) as tar:
        return {member.name: (member, tar.extractfile(member).read()) for member in tar.getmembers()}


def test_app01_archive_has_only_app_md_and_wheel_and_returns_wheel_sha256():
    wheel = _wheel()
    archive, sha256 = build_app_archive(_app(wheel=wheel))
    members = _members(archive)
    assert sorted(members) == ["APP.md", WHEEL_NAME]
    assert members[WHEEL_NAME][1] == wheel
    assert members["APP.md"][1].decode("utf-8") == render_app_md(_app(wheel=wheel))
    assert all(info.mode == 0o644 and info.mtime == 0 for info, _ in members.values())
    assert sha256 == hashlib.sha256(wheel).hexdigest()


def test_app01_app_md_golden():
    card = _card(
        settings=[
            _setting(file=".claude/apis/redmine.env", key="REDMINE_URL"),
            _setting("REDMINE_API_KEY", description="Sua chave de API do Redmine", secret=True),
            {"name": "PORTA", "description": "Porta local", "required": False, "secret": False, "file": ".horas.env", "key": "PORTA"},
        ]
    )
    assert render_app_md(_app(card)) == (
        "# Controle de horas\n"
        "\n"
        "Controle de horas local com lançamento no Redmine.\n"
        "\n"
        "## Como usar\n"
        "\n"
        "Inicie com `horas-app` e confira com `horas-app verificar`.\n"
        "\n"
        "## Comandos\n"
        "\n"
        "- Iniciar: `horas-app`\n"
        "- Verificar: `horas-app verificar`\n"
        "\n"
        "## Requisitos\n"
        "\n"
        "- Python 3.11 ou mais\n"
        "\n"
        "## O que precisa configurar\n"
        "\n"
        "- `REDMINE_URL`: Endereço do Redmine (obrigatório, variável REDMINE_URL ou chave REDMINE_URL em ~/.claude/apis/redmine.env)\n"
        "- `REDMINE_API_KEY`: Sua chave de API do Redmine (obrigatório, segredo, variável REDMINE_API_KEY)\n"
        "- `PORTA`: Porta local (opcional, chave PORTA em ~/.horas.env)\n"
    )


def test_app01_app_md_without_title_requirements_and_settings():
    card = _card(settings=[], requirements=[])
    card.pop("title")
    app_md = render_app_md(_app(card))
    assert app_md.startswith("# horas-app\n")
    assert "## Requisitos" not in app_md
    assert app_md.endswith("## O que precisa configurar\n\nNada a configurar.\n")


def test_app01_same_input_builds_same_archive():
    first, _ = build_app_archive(_app())
    second, _ = build_app_archive(_app())
    assert hashlib.sha256(first).hexdigest() == hashlib.sha256(second).hexdigest()


def test_app01_archive_above_store_limit_is_rejected_with_413(monkeypatch):
    monkeypatch.setattr("app.services.skill_packages.MAX_ARCHIVE_BYTES", 10)
    with pytest.raises(AppRuleError) as error:
        build_app_archive(_app())
    assert error.value.status_code == 413


def test_app01_file_with_double_dot_inside_a_name_is_accepted():
    app = _app(_card(settings=[_setting(file=".claude/a..b.env", key="K")]))
    assert app.settings[0].file == ".claude/a..b.env"


@pytest.mark.parametrize("field, card", [
    ("title", _card(title="Horas\n## injetado")),
    ("title", _card(title="Horas\r## injetado")),
    ("requirements", _card(requirements=["Python\n## injetado"])),
])
def test_app01_multiline_title_or_requirement_is_rejected(field, card):
    _rejects(f"^{field} deve ter uma linha só$", card=card)


@pytest.mark.parametrize("command", ["/horas-app", "horas-app  verificar", " horas-app", "horas-app "])
def test_app01_command_with_leading_slash_or_extra_spaces_is_rejected(command):
    _rejects("commands.start", card=_card(commands={"start": command, "verify": "horas-app verificar"}))


def test_app01_command_with_dot_in_program_name_is_accepted():
    assert _app(_card(commands={"start": "horas.app", "verify": "horas_app verificar"})).commands.start == "horas.app"


@pytest.mark.parametrize(
    "wheel_name",
    ["horas_app-0.1.0-1-py3-none-any.whl", "horas_app-1.0+local-py3-none-any.whl", "horas_app-1!2.0-py3-none-any.whl", "Horas.App-0.1.0-py3-none-any.whl"],
)
def test_app01_wheel_file_name_with_build_tag_or_local_version_is_accepted(wheel_name):
    assert _app(wheel_name=wheel_name).wheel_name == wheel_name


@pytest.mark.parametrize("wheel_name", ["horas_app-0.1.0-py3-none.whl", "horas_app-0.1.0-x-py3-none-any.whl", "horas_app-0.1.0-py3-none-any-x.whl"])
def test_app01_wheel_file_name_with_wrong_tags_is_rejected(wheel_name):
    _rejects("precisa ser um .whl sem caminho", wheel_name=wheel_name)


def test_app01_wheel_file_project_must_match_app_name():
    _rejects("^O .whl é do projeto outro_app, não de horas-app$", wheel_name="outro_app-0.1.0-py3-none-any.whl")


@pytest.mark.parametrize(
    "wheel, match",
    [
        (_wheel(metadata_path="horas_app-0.1.0-dist-info/METADATA"), "^Arquivo .whl sem METADATA$"),
        (_wheel(metadata_path="horas_app-0.1.0.dist-info/METADATA.bak"), "^Arquivo .whl sem METADATA$"),
        (_wheel(raw_metadata=b"Version: 0.1.0\nRequires-Python: >=3.11\n"), "^METADATA do .whl sem Name$"),
        (_wheel(raw_metadata=b"Name: horas-app\nVersion: \nRequires-Python: >=3.11\n"), "^METADATA do .whl sem Version$"),
    ],
    ids=["dist-info-sem-ponto", "metadata-com-sufixo", "sem-name", "version-vazio"],
)
def test_app01_metadata_without_required_field_is_rejected(wheel, match):
    _rejects(match, wheel=wheel)


def test_app01_metadata_with_exactly_1_mib_is_accepted():
    header = b"Name: horas-app\nVersion: 0.1.0\nRequires-Python: >=3.11\nSummary: "
    raw = header + b"x" * ((1 << 20) - len(header) - 1) + b"\n"
    assert len(raw) == 1 << 20
    assert _app(wheel=_wheel(raw_metadata=raw)).python_requires == ">=3.11"
