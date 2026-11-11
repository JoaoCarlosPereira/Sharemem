import email
import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from app.services.script_packages import ScriptInput

SCRIPT = Path(__file__).resolve().parents[1] / "store_scripts" / "publicar-app.py"
TOKEN = "omtk_token_secreto"


def _carregar():
    spec = importlib.util.spec_from_file_location("publicar_app", SCRIPT)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


publicar_app = _carregar()


class StoreFalsa:
    def __init__(self, status=200, resposta=None):
        self.status = status
        self.resposta = resposta or {"name": "horas-app", "version": "1", "sha256": "abc", "created": True}
        self.pedidos = []
        loja = self

        class Handler(BaseHTTPRequestHandler):
            def do_PUT(self):
                corpo = self.rfile.read(int(self.headers["Content-Length"]))
                loja.pedidos.append((self.path, dict(self.headers), corpo))
                saida = json.dumps(loja.resposta).encode()
                self.send_response(loja.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(saida)))
                self.end_headers()
                self.wfile.write(saida)

            def log_message(self, *args):
                pass

        self.servidor = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.servidor.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.servidor.server_port}"

    def parar(self):
        self.servidor.shutdown()


@pytest.fixture
def pasta_app(tmp_path):
    (tmp_path / "app.json").write_text(json.dumps({"name": "horas-app", "usage": "horas-app"}), encoding="utf-8")
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "horas_app-0.1.0-py3-none-any.whl").write_bytes(b"PK-conteudo-do-wheel")
    return tmp_path


@pytest.fixture
def store():
    loja = StoreFalsa()
    yield loja
    loja.parar()


def _partes(cabecalhos, corpo):
    mensagem = email.message_from_bytes(
        f"Content-Type: {cabecalhos['Content-Type']}\r\n\r\n".encode() + corpo
    )
    return {parte.get_param("name", header="content-disposition"): parte for parte in mensagem.get_payload()}


def test_app01_publica_multipart_com_card_wheel_e_token(pasta_app, store, monkeypatch, capsys):
    monkeypatch.setenv("SHAREMEM_TOKEN", TOKEN)

    codigo = publicar_app.publicar(pasta_app, store.url)

    assert codigo == 0
    caminho, cabecalhos, corpo = store.pedidos[0]
    assert caminho == "/api/v1/store/apps/horas-app"
    assert cabecalhos["Authorization"] == f"Bearer {TOKEN}"
    partes = _partes(cabecalhos, corpo)
    assert json.loads(partes["card"].get_payload(decode=True)) == {"name": "horas-app", "usage": "horas-app"}
    assert partes["wheel"].get_filename() == "horas_app-0.1.0-py3-none-any.whl"
    assert partes["wheel"].get_payload(decode=True) == b"PK-conteudo-do-wheel"


def test_app01_imprime_nome_versao_sha_e_situacao(pasta_app, store, monkeypatch, capsys):
    monkeypatch.setenv("SHAREMEM_TOKEN", TOKEN)

    publicar_app.publicar(pasta_app, store.url)

    assert capsys.readouterr().out.strip() == "horas-app versão 1 (versão nova) sha256 abc"


def test_app01_wheel_igual_ao_publicado_avisa_sem_mudanca(pasta_app, monkeypatch, capsys):
    monkeypatch.setenv("SHAREMEM_TOKEN", TOKEN)
    loja = StoreFalsa(resposta={"name": "horas-app", "version": "4", "sha256": "abc", "created": False})
    try:
        codigo = publicar_app.publicar(pasta_app, loja.url)
    finally:
        loja.parar()

    assert codigo == 0
    assert capsys.readouterr().out.strip() == "horas-app versão 4 (sem mudança) sha256 abc"


def test_app01_sem_token_para_antes_de_chamar(pasta_app, store, monkeypatch):
    monkeypatch.delenv("SHAREMEM_TOKEN", raising=False)

    with pytest.raises(SystemExit, match="SHAREMEM_TOKEN"):
        publicar_app.publicar(pasta_app, store.url)

    assert store.pedidos == []


@pytest.mark.parametrize("quantidade", [0, 2])
def test_app01_sem_ou_com_dois_whl_para(pasta_app, store, monkeypatch, quantidade):
    monkeypatch.setenv("SHAREMEM_TOKEN", TOKEN)
    for wheel in (pasta_app / "dist").glob("*.whl"):
        wheel.unlink()
    for numero in range(quantidade):
        (pasta_app / "dist" / f"horas_app-0.{numero}-py3-none-any.whl").write_bytes(b"x")

    with pytest.raises(SystemExit, match=f"achei {quantidade}"):
        publicar_app.publicar(pasta_app, store.url)

    assert store.pedidos == []


def test_app01_sem_app_json_para(pasta_app, store, monkeypatch):
    monkeypatch.setenv("SHAREMEM_TOKEN", TOKEN)
    (pasta_app / "app.json").unlink()

    with pytest.raises(SystemExit, match="app.json"):
        publicar_app.publicar(pasta_app, store.url)


def test_app01_recusa_da_store_mostra_detalhe_e_sai_1(pasta_app, monkeypatch, capsys):
    monkeypatch.setenv("SHAREMEM_TOKEN", TOKEN)
    loja = StoreFalsa(status=422, resposta={"detail": "Campo obrigatório ausente: usage"})
    try:
        codigo = publicar_app.publicar(pasta_app, loja.url)
    finally:
        loja.parar()

    assert codigo == 1
    assert capsys.readouterr().err.strip() == "A Store recusou (422): Campo obrigatório ausente: usage"


def test_app01_store_fora_do_ar_sai_1(pasta_app, monkeypatch, capsys):
    monkeypatch.setenv("SHAREMEM_TOKEN", TOKEN)

    codigo = publicar_app.publicar(pasta_app, "http://127.0.0.1:1")

    assert codigo == 1
    assert "Store indisponível" in capsys.readouterr().err


def test_app01_nenhuma_saida_mostra_o_token(pasta_app, monkeypatch, capsys):
    monkeypatch.setenv("SHAREMEM_TOKEN", TOKEN)
    for status in (200, 422):
        loja = StoreFalsa(status=status, resposta={"detail": "x", "name": "a", "version": "1", "sha256": "s"})
        try:
            publicar_app.publicar(pasta_app, loja.url)
        finally:
            loja.parar()
    saida = capsys.readouterr()
    assert TOKEN not in saida.out and TOKEN not in saida.err


def test_app01_conteudo_do_script_passa_nas_regras_da_store():
    ScriptInput(
        name="publicar-app",
        file_name="publicar-app.py",
        description="Publica na Store do ShareMem o app do diretório atual (app.json + dist/*.whl).",
        usage="SHAREMEM_TOKEN=<token> python publicar-app.py --pasta <repositório do app>",
        language="python",
        effects=["acessa-rede"],
        area="sharemem",
        content=SCRIPT.read_text(encoding="utf-8"),
    )
