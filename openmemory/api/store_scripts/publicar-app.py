import argparse
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

STORE_PADRAO = "http://192.168.3.213:8765"


def montar_multipart(card: bytes, wheel_name: str, wheel: bytes, boundary: str) -> bytes:
    partes = [
        (b'Content-Disposition: form-data; name="card"\r\nContent-Type: application/json', card),
        (
            f'Content-Disposition: form-data; name="wheel"; filename="{wheel_name}"\r\n'
            "Content-Type: application/octet-stream".encode("utf-8"),
            wheel,
        ),
    ]
    corpo = b""
    for cabecalho, conteudo in partes:
        corpo += b"--" + boundary.encode("ascii") + b"\r\n" + cabecalho + b"\r\n\r\n" + conteudo + b"\r\n"
    return corpo + b"--" + boundary.encode("ascii") + b"--\r\n"


def _ler_pacote(pasta: Path) -> tuple[bytes, Path]:
    ficha = pasta / "app.json"
    if not ficha.is_file():
        raise SystemExit(f"Não encontrei {ficha}")
    wheels = sorted((pasta / "dist").glob("*.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"Preciso de exatamente um .whl em {pasta / 'dist'}; achei {len(wheels)}")
    return ficha.read_bytes(), wheels[0]


def publicar(pasta: Path, store: str) -> int:
    token = os.environ.get("SHAREMEM_TOKEN")
    if not token:
        raise SystemExit("Defina a variável SHAREMEM_TOKEN com o seu token do ShareMem")
    card, wheel = _ler_pacote(pasta)
    nome = json.loads(card)["name"]
    boundary = uuid.uuid4().hex
    requisicao = urllib.request.Request(
        f"{store.rstrip('/')}/api/v1/store/apps/{nome}",
        data=montar_multipart(card, wheel.name, wheel.read_bytes(), boundary),
        method="PUT",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    try:
        with urllib.request.urlopen(requisicao, timeout=60) as resposta:
            dados = json.loads(resposta.read())
    except urllib.error.HTTPError as erro:
        print(f"A Store recusou ({erro.code}): {_detalhe(erro.read())}", file=sys.stderr)
        return 1
    except urllib.error.URLError as erro:
        print(f"Store indisponível em {store}: {erro.reason}", file=sys.stderr)
        return 1
    situacao = "versão nova" if dados.get("created") else "sem mudança"
    print(f"{dados['name']} versão {dados['version']} ({situacao}) sha256 {dados['sha256']}")
    return 0


def _detalhe(corpo: bytes) -> str:
    try:
        return str(json.loads(corpo).get("detail"))
    except (ValueError, AttributeError):
        return corpo.decode("utf-8", "replace")


def main() -> int:
    parser = argparse.ArgumentParser(description="Publica o app do diretório atual na Store do ShareMem")
    parser.add_argument("--store", default=STORE_PADRAO, help="Endereço da Store")
    parser.add_argument("--pasta", default=".", help="Diretório com app.json e dist/*.whl")
    args = parser.parse_args()
    return publicar(Path(args.pasta), args.store)


if __name__ == "__main__":
    sys.exit(main())
