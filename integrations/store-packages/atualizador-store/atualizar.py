from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import socket
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

MARCADOR = ".sharemem-store.json"
ESTADO = ".sharemem-store-update.json"
TRAVA = ".sharemem-store-update.lock"
INTERVALO_PADRAO = 6 * 3600
TRAVA_VENCIDA = 120
BASE_PADRAO = "http://192.168.3.213:8765"
TIMEOUT = 5
PRAZO_TOTAL = 20
TIPOS = {"hook": ("hooks", "HOOK.md"), "skill": ("skills", "SKILL.md")}


class Falha(Exception):
    pass


class Limite(Falha):
    pass


@dataclass
class Pacote:
    kind: str
    pasta: Path
    marcador: dict | None


@dataclass
class Remoto:
    digest: str
    endpoint: str
    eventos: dict | None


@dataclass
class Resultado:
    atualizados: list = field(default_factory=list)
    adotados: list = field(default_factory=list)
    avisos: list = field(default_factory=list)


def ler_json(caminho):
    try:
        return json.loads(Path(caminho).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def gravar_json(caminho, dados):
    Path(caminho).write_text(json.dumps(dados, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def config_mcp(claude_json):
    servidores = (claude_json or {}).get("mcpServers") or {}
    return servidores.get("mem0") or servidores.get("sharemem") or {}


def base_url(claude_json, env):
    if env.get("OPENMEMORY_API_BASE"):
        return env["OPENMEMORY_API_BASE"].rstrip("/")
    url = config_mcp(claude_json).get("url")
    if isinstance(url, str):
        partes = urllib.parse.urlsplit(url)
        if partes.scheme and partes.netloc:
            return f"{partes.scheme}://{partes.netloc}"
    return BASE_PADRAO


def agent_token(claude_json):
    url = config_mcp(claude_json).get("url")
    if not isinstance(url, str):
        return None
    valores = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("token") or [None]
    token = valores[0]
    return token if token and token.startswith("omtk_") else None


def sha256(dados):
    return hashlib.sha256(dados).hexdigest()


def pacotes_instalados(claude_dir):
    encontrados = []
    for kind, (subpasta, arquivo_raiz) in TIPOS.items():
        base = Path(claude_dir) / subpasta
        if not base.is_dir():
            continue
        for pasta in sorted(base.iterdir()):
            if not pasta.is_dir() or pasta.name.startswith("."):
                continue
            marcador = ler_json(pasta / MARCADOR)
            if isinstance(marcador, dict) and marcador.get("kind") == kind and marcador.get("name"):
                encontrados.append(Pacote(kind, pasta, marcador))
            elif kind == "hook" or (pasta / arquivo_raiz).is_file():
                encontrados.append(Pacote(kind, pasta, None))
    return encontrados


def ler_pacote(dados):
    arquivos = {}
    with tarfile.open(fileobj=io.BytesIO(dados), mode="r:gz") as tar:
        for membro in tar.getmembers():
            if membro.isdir():
                continue
            nome = PurePosixPath(membro.name)
            if not membro.isfile() or nome.is_absolute() or ".." in nome.parts or not nome.parts:
                raise Falha(f"entrada recusada no pacote: {membro.name}")
            conteudo = tar.extractfile(membro).read()
            arquivos[nome.as_posix()] = (conteudo, membro.mode & 0o777 or 0o644)
    return arquivos


def hashes_locais(pasta, nomes):
    hashes = {}
    for nome in nomes:
        arquivo = Path(pasta) / nome
        if arquivo.is_file():
            hashes[nome] = sha256(arquivo.read_bytes())
    return hashes


def arquivos_do_usuario(pasta):
    pasta = Path(pasta)
    for arquivo in sorted(pasta.rglob("*")):
        relativo = arquivo.relative_to(pasta).as_posix()
        if arquivo.is_file() and relativo != MARCADOR and "__pycache__" not in arquivo.parts:
            yield relativo, arquivo


def trocar_pasta(destino, novos, preservados):
    destino = Path(destino)
    temporaria = Path(tempfile.mkdtemp(prefix=f".{destino.name}-novo-", dir=destino.parent))
    try:
        for nome, (conteudo, modo) in novos.items():
            alvo = temporaria / nome
            alvo.parent.mkdir(parents=True, exist_ok=True)
            alvo.write_bytes(conteudo)
            os.chmod(alvo, modo)
        for nome, origem in preservados.items():
            alvo = temporaria / nome
            alvo.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(origem, alvo)
        antiga = destino.parent / f".{destino.name}-antigo-{time.time_ns()}"
        destino.rename(antiga)
        try:
            temporaria.rename(destino)
        except OSError:
            antiga.rename(destino)
            raise
        shutil.rmtree(antiga, ignore_errors=True)
    except BaseException:
        shutil.rmtree(temporaria, ignore_errors=True)
        raise


def novo_marcador(pacote, nome, tag, remoto, arquivos_hash):
    return {
        "kind": pacote.kind,
        "name": nome,
        "tag": tag,
        "digest": remoto.digest,
        "eventos": remoto.eventos,
        "arquivos": arquivos_hash,
        "atualizado_em": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


class Http:
    def __init__(self, base, token, prazo, maquina=None):
        self.base = base
        self.token = token
        self.prazo = prazo
        self.maquina = maquina or socket.gethostname()

    def _abrir(self, requisicao):
        restante = self.prazo - time.monotonic()
        if restante <= 0:
            raise Falha("prazo esgotado")
        if self.token:
            requisicao.add_header("Authorization", f"Bearer {self.token}")
        requisicao.add_header("X-Hostname", self.maquina)
        try:
            with urllib.request.urlopen(requisicao, timeout=min(TIMEOUT, restante)) as resposta:
                return resposta.read()
        except urllib.error.HTTPError as erro:
            if erro.code == 404:
                return None
            if erro.code == 429:
                raise Limite("limite de requisições do servidor") from erro
            raise Falha(f"HTTP {erro.code}") from erro
        except (urllib.error.URLError, OSError) as erro:
            raise Falha(str(erro)) from erro

    def atualizacoes(self, pacotes):
        itens = [{"kind": kind, "name": nome, "tag": tag} for kind, nome, tag in pacotes]
        requisicao = urllib.request.Request(
            f"{self.base}/api/v1/store/updates",
            data=json.dumps({"target": "claude", "packages": itens}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        bruto = self._abrir(requisicao)
        if bruto is None:
            raise Falha("servidor sem a rota de atualizações")
        respostas = {}
        for item in json.loads(bruto).get("packages") or []:
            if item.get("found"):
                respostas[(item.get("kind"), item.get("name"))] = item.get("recipe")
        return respostas

    def artefato(self, endpoint):
        return self._abrir(urllib.request.Request(f"{self.base}{endpoint}"))


def remoto_da_receita(resposta):
    if not isinstance(resposta, dict):
        return None
    receita = resposta.get("recipe", resposta)
    fonte = receita.get("source") or {}
    if fonte.get("type") != "registry_artifact" or not fonte.get("artifact_digest"):
        return None
    eventos = None
    for passo in receita.get("steps") or []:
        if passo.get("type") == "merge_hooks":
            eventos = (passo.get("content") or {}).get("hooks")
    return Remoto(fonte["artifact_digest"], fonte["endpoint"], eventos)


def pode_adotar(kind, locais, esperados):
    if not locais or any(locais[nome] != esperados[nome] for nome in locais):
        return False
    return kind != "skill" or TIPOS["skill"][1] in locais


def baixar(http, remoto):
    dados = http.artefato(remoto.endpoint)
    if dados is None:
        raise Falha("artefato não encontrado")
    if sha256(dados) != remoto.digest:
        raise Falha("sha256 do artefato não confere")
    return ler_pacote(dados)


def identidade(pacote):
    nome = (pacote.marcador or {}).get("name") or pacote.pasta.name
    tag = (pacote.marcador or {}).get("tag") or "latest"
    return pacote.kind, nome, tag


def verificar_pacote(http, pacote, receita, resultado, avisados):
    _, nome, tag = identidade(pacote)
    rotulo = f"{pacote.kind} {nome}"
    remoto = remoto_da_receita(receita)
    if remoto is None:
        return

    def avisar(chave, texto):
        if avisados.get(chave) != remoto.digest:
            resultado.avisos.append(texto)
            avisados[chave] = remoto.digest

    marcador = pacote.marcador
    if marcador is None:
        arquivos = baixar(http, remoto)
        locais = hashes_locais(pacote.pasta, arquivos)
        esperados = {nome_arq: sha256(conteudo) for nome_arq, (conteudo, _) in arquivos.items()}
        if pode_adotar(pacote.kind, locais, esperados):
            gravar_json(pacote.pasta / MARCADOR, novo_marcador(pacote, nome, tag, remoto, esperados))
            resultado.adotados.append(rotulo)
        else:
            avisar(f"adocao:{rotulo}", f"{rotulo} difere da versão da Store; reinstale pela Store para receber atualizações automáticas.")
        return

    if marcador.get("digest") == remoto.digest:
        if not marcador.get("arquivos"):
            arquivos = baixar(http, remoto)
            marcador["arquivos"] = {n: sha256(c) for n, (c, _) in arquivos.items()}
            marcador["eventos"] = remoto.eventos
            gravar_json(pacote.pasta / MARCADOR, marcador)
        return

    if pacote.kind == "hook" and marcador.get("eventos") is not None and marcador.get("eventos") != remoto.eventos:
        avisar(f"eventos:{rotulo}", f"{rotulo} tem versão nova que muda os eventos do settings.json; reinstale pela Store (get_install_recipe kind=hook name={nome}).")
        return

    instalados = marcador.get("arquivos")
    if not instalados:
        avisar(f"sem-hashes:{rotulo}", f"{rotulo} tem versão nova, mas a instalação não registrou os arquivos; reinstale pela Store.")
        return

    arquivos = baixar(http, remoto)
    preservados = {}
    alterados = []
    for relativo, caminho in arquivos_do_usuario(pacote.pasta):
        original = instalados.get(relativo)
        if original is None:
            preservados[relativo] = caminho
        elif sha256(caminho.read_bytes()) != original:
            preservados[relativo] = caminho
            alterados.append(relativo)
    novos = {n: v for n, v in arquivos.items() if n not in preservados}
    hashes = {n: sha256(c) for n, (c, _) in arquivos.items()}
    for relativo in alterados:
        if relativo in hashes:
            hashes[relativo] = instalados[relativo]
    trocar_pasta(pacote.pasta, novos, preservados)
    gravar_json(pacote.pasta / MARCADOR, novo_marcador(pacote, nome, tag, remoto, hashes))
    resultado.atualizados.append(rotulo)
    if alterados:
        avisar(f"alterados:{rotulo}", f"{rotulo} atualizado mantendo arquivos alterados por você: {', '.join(alterados)}.")


def pegar_trava(claude_dir, agora):
    trava = Path(claude_dir) / TRAVA
    try:
        if trava.exists() and agora - trava.stat().st_mtime > TRAVA_VENCIDA:
            trava.unlink()
        os.close(os.open(trava, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return trava
    except OSError:
        return None


def executar(claude_dir, claude_json, env, http=None, agora=None):
    claude_dir = Path(claude_dir)
    agora = time.time() if agora is None else agora
    estado = ler_json(claude_dir / ESTADO) or {}
    intervalo = int(env.get("SHAREMEM_STORE_INTERVALO", INTERVALO_PADRAO))
    if env.get("SHAREMEM_STORE_FORCAR") != "1" and agora - estado.get("ultima_verificacao", 0) < intervalo:
        return None
    trava = pegar_trava(claude_dir, agora)
    if trava is None:
        return None
    try:
        http = http or Http(base_url(claude_json, env), agent_token(claude_json), time.monotonic() + PRAZO_TOTAL)
        resultado = Resultado()
        avisados = estado.get("avisados") or {}
        pacotes = pacotes_instalados(claude_dir)
        concluida = True
        try:
            receitas = http.atualizacoes([identidade(p) for p in pacotes]) if pacotes else {}
        except Falha:
            receitas = {}
            concluida = False
        for pacote in pacotes:
            kind, nome, _ = identidade(pacote)
            if (kind, nome) not in receitas:
                continue
            try:
                verificar_pacote(http, pacote, receitas[(kind, nome)], resultado, avisados)
            except Limite:
                concluida = False
                break
            except Falha:
                continue
        if concluida:
            estado["ultima_verificacao"] = agora
        else:
            estado.pop("ultima_verificacao", None)
        estado["avisados"] = avisados
        gravar_json(claude_dir / ESTADO, estado)
        return resultado
    finally:
        trava.unlink(missing_ok=True)


def mensagem(resultado):
    if resultado is None:
        return None
    partes = []
    if resultado.atualizados:
        partes.append("Atualizados pela Store: " + ", ".join(resultado.atualizados) + ".")
    if resultado.adotados:
        partes.append("Passaram a receber atualizações da Store: " + ", ".join(resultado.adotados) + ".")
    partes.extend(resultado.avisos)
    return " ".join(partes) or None


def main():
    home = Path.home()
    texto = None
    try:
        texto = mensagem(executar(home / ".claude", ler_json(home / ".claude.json"), dict(os.environ)))
    except Exception:
        texto = None
    if texto:
        sys.stdout.write(json.dumps({
            "systemMessage": texto,
            "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": texto},
        }))


if __name__ == "__main__":
    main()
