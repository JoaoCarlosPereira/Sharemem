import hashlib
import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import atualizar as at


def pacote_tgz(arquivos, modos=None):
    saida = io.BytesIO()
    with tarfile.open(fileobj=saida, mode="w:gz") as tar:
        for nome, conteudo in arquivos.items():
            info = tarfile.TarInfo(nome)
            info.size = len(conteudo)
            info.mode = (modos or {}).get(nome, 0o644)
            tar.addfile(info, io.BytesIO(conteudo))
    return saida.getvalue()


EVENTOS = {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": 'node "${HOOK_DIR}/h.mjs"'}]}]}


class LojaFalsa:
    def __init__(self):
        self.publicados = {}
        self.downloads = 0

    def publicar(self, kind, nome, arquivos, eventos=EVENTOS, adulterar=False):
        dados = pacote_tgz(arquivos)
        digest = hashlib.sha256(dados).hexdigest()
        if adulterar:
            dados = dados + b"x"
        self.publicados[(kind, nome)] = (dados, digest, eventos if kind == "hook" else None)
        return digest

    def receita(self, kind, nome, tag):
        if (kind, nome) not in self.publicados:
            return None
        _, digest, eventos = self.publicados[(kind, nome)]
        passos = [{"type": "download_and_extract"}]
        if eventos is not None:
            passos.append({"type": "merge_hooks", "content": {"hooks": eventos}})
        return {"recipe": {
            "source": {"type": "registry_artifact", "artifact_digest": digest, "endpoint": f"/a/{kind}/{nome}"},
            "steps": passos,
        }}

    def artefato(self, endpoint):
        self.downloads += 1
        _, kind, nome = endpoint.rsplit("/", 2)
        return self.publicados[(kind, nome)][0]


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.claude = Path(self._tmp.name)
        self.loja = LojaFalsa()
        self.agora = 1_000_000.0

    def tearDown(self):
        self._tmp.cleanup()

    def instalar_manual(self, kind, nome, arquivos):
        pasta = self.claude / at.TIPOS[kind][0] / nome
        pasta.mkdir(parents=True)
        for relativo, conteudo in arquivos.items():
            (pasta / relativo).parent.mkdir(parents=True, exist_ok=True)
            (pasta / relativo).write_bytes(conteudo)
        return pasta

    def rodar(self, **env):
        env.setdefault("SHAREMEM_STORE_FORCAR", "1")
        self.agora += 1
        return at.executar(self.claude, {}, env, http=self.loja, agora=self.agora)

    def marcador(self, pasta):
        return json.loads((pasta / at.MARCADOR).read_text(encoding="utf-8"))


class TestAdocao(Base):
    def test_instalacao_identica_a_store_passa_a_ser_atualizada(self):
        v1 = {"HOOK.md": b"# v1", "h.mjs": b"1"}
        self.loja.publicar("hook", "recall", v1)
        pasta = self.instalar_manual("hook", "recall", v1)

        resultado = self.rodar()

        self.assertEqual(resultado.adotados, ["hook recall"])
        marcador = self.marcador(pasta)
        self.assertEqual(marcador["eventos"], EVENTOS)
        self.assertEqual(set(marcador["arquivos"]), {"HOOK.md", "h.mjs"})

    def test_instalacao_diferente_so_avisa_uma_vez(self):
        self.loja.publicar("hook", "recall", {"HOOK.md": b"# v2", "h.mjs": b"2"})
        pasta = self.instalar_manual("hook", "recall", {"HOOK.md": b"# v1", "h.mjs": b"1"})

        primeiro = self.rodar()
        segundo = self.rodar()

        self.assertEqual(len(primeiro.avisos), 1)
        self.assertEqual(segundo.avisos, [])
        self.assertFalse((pasta / at.MARCADOR).exists())
        self.assertEqual((pasta / "h.mjs").read_bytes(), b"1")

    def test_pasta_que_nao_existe_na_store_e_ignorada(self):
        pasta = self.instalar_manual("skill", "commit", {"SKILL.md": b"# minha"})
        resultado = self.rodar()
        self.assertEqual((resultado.adotados, resultado.avisos), ([], []))
        self.assertFalse((pasta / at.MARCADOR).exists())

    def test_hook_instalado_pelo_instalador_antigo_sem_hook_md_e_adotado(self):
        publicado = {"HOOK.md": b"# v1", "instalar.py": b"i", "verificar.py": b"v", "config.json": b"{}"}
        self.loja.publicar("hook", "bloqueio", publicado)
        pasta = self.instalar_manual("hook", "bloqueio", {"verificar.py": b"v", "config.json": b"{}"})

        self.assertEqual(self.rodar().adotados, ["hook bloqueio"])

        self.loja.publicar("hook", "bloqueio", {**publicado, "HOOK.md": b"# v2", "verificar.py": b"v2"})
        self.assertEqual(self.rodar().atualizados, ["hook bloqueio"])
        self.assertEqual((pasta / "verificar.py").read_bytes(), b"v2")
        self.assertEqual((pasta / "HOOK.md").read_bytes(), b"# v2")

    def test_pasta_vazia_nao_e_adotada(self):
        self.loja.publicar("hook", "vazio", {"HOOK.md": b"#"})
        pasta = self.instalar_manual("hook", "vazio", {})
        self.assertEqual(self.rodar().adotados, [])
        self.assertFalse((pasta / at.MARCADOR).exists())

    def test_skill_tambem_e_adotada(self):
        arquivos = {"SKILL.md": b"# skill", "references/a.md": b"a"}
        self.loja.publicar("skill", "tarefa", arquivos)
        self.instalar_manual("skill", "tarefa", arquivos)
        self.assertEqual(self.rodar().adotados, ["skill tarefa"])


class TestAtualizacao(Base):
    def instalar_v1(self):
        v1 = {"HOOK.md": b"# v1", "h.mjs": b"1", "config.json": b"{}", "velho.py": b"x"}
        self.loja.publicar("hook", "recall", v1)
        pasta = self.instalar_manual("hook", "recall", v1)
        self.rodar()
        return pasta

    def test_versao_nova_troca_arquivos_e_preserva_os_do_usuario(self):
        pasta = self.instalar_v1()
        (pasta / "config.json").write_bytes(b'{"modo": "aviso"}')
        (pasta / "meu.txt").write_bytes(b"nota")
        digest = self.loja.publicar("hook", "recall", {"HOOK.md": b"# v2", "h.mjs": b"2", "config.json": b"{\"novo\": 1}"})

        resultado = self.rodar()

        self.assertEqual(resultado.atualizados, ["hook recall"])
        self.assertEqual((pasta / "h.mjs").read_bytes(), b"2")
        self.assertEqual((pasta / "config.json").read_bytes(), b'{"modo": "aviso"}')
        self.assertEqual((pasta / "meu.txt").read_bytes(), b"nota")
        self.assertFalse((pasta / "velho.py").exists())
        self.assertIn("config.json", resultado.avisos[0])
        marcador = self.marcador(pasta)
        self.assertEqual(marcador["digest"], digest)
        self.assertNotEqual(marcador["arquivos"]["config.json"], hashlib.sha256(b'{"novo": 1}').hexdigest())
        self.assertEqual([p.name for p in pasta.parent.iterdir()], ["recall"])

    def test_mesma_versao_nao_baixa_de_novo(self):
        self.instalar_v1()
        downloads = self.loja.downloads
        resultado = self.rodar()
        self.assertEqual(self.loja.downloads, downloads)
        self.assertEqual(resultado.atualizados, [])

    def test_eventos_alterados_nao_atualiza_e_avisa(self):
        pasta = self.instalar_v1()
        outros = {"SessionStart": [{"hooks": [{"type": "command", "command": "x"}]}]}
        self.loja.publicar("hook", "recall", {"HOOK.md": b"# v2", "h.mjs": b"2"}, eventos=outros)

        resultado = self.rodar()

        self.assertEqual(resultado.atualizados, [])
        self.assertIn("eventos", resultado.avisos[0])
        self.assertEqual((pasta / "h.mjs").read_bytes(), b"1")

    def test_sha256_errado_nao_toca_na_instalacao(self):
        pasta = self.instalar_v1()
        self.loja.publicar("hook", "recall", {"HOOK.md": b"# v2", "h.mjs": b"2"}, adulterar=True)
        resultado = self.rodar()
        self.assertEqual(resultado.atualizados, [])
        self.assertEqual((pasta / "h.mjs").read_bytes(), b"1")

    def test_marcador_da_receita_sem_hashes_e_completado(self):
        v1 = {"SKILL.md": b"# s"}
        digest = self.loja.publicar("skill", "s", v1)
        pasta = self.instalar_manual("skill", "s", v1)
        (pasta / at.MARCADOR).write_text(json.dumps({"kind": "skill", "name": "s", "tag": "latest", "digest": digest}))

        self.rodar()

        self.assertEqual(set(self.marcador(pasta)["arquivos"]), {"SKILL.md"})


class TestSeguranca(Base):
    def test_entrada_com_caminho_para_fora_e_recusada(self):
        with self.assertRaises(at.Falha):
            at.ler_pacote(pacote_tgz({"../fora.txt": b"x"}))

    def test_link_simbolico_e_recusado(self):
        saida = io.BytesIO()
        with tarfile.open(fileobj=saida, mode="w:gz") as tar:
            info = tarfile.TarInfo("link")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tar.addfile(info)
        with self.assertRaises(at.Falha):
            at.ler_pacote(saida.getvalue())

    def test_respeita_o_intervalo_entre_verificacoes(self):
        self.loja.publicar("hook", "recall", {"HOOK.md": b"#"})
        self.rodar()
        self.assertIsNone(self.rodar(SHAREMEM_STORE_FORCAR="0"))

    def test_trava_de_outra_sessao_impede_execucao_simultanea(self):
        (self.claude / at.TRAVA).write_text("")
        self.assertIsNone(at.executar(self.claude, {}, {"SHAREMEM_STORE_FORCAR": "1"}, http=self.loja, agora=self.claude.stat().st_mtime + 1))


class TestConfiguracao(unittest.TestCase):
    URL = "http://127.0.0.1:8766/mcp/claude-code/http/S0360?token=omtk_abc&group=Fiscal"

    def test_base_e_token_vem_da_url_do_mcp(self):
        claude_json = {"mcpServers": {"mem0": {"url": self.URL}}}
        self.assertEqual(at.base_url(claude_json, {}), "http://127.0.0.1:8766")
        self.assertEqual(at.agent_token(claude_json), "omtk_abc")

    def test_variavel_de_ambiente_vence_e_sem_mcp_usa_o_padrao(self):
        self.assertEqual(at.base_url({}, {"OPENMEMORY_API_BASE": "http://x:1/"}), "http://x:1")
        self.assertEqual(at.base_url({}, {}), at.BASE_PADRAO)
        self.assertIsNone(at.agent_token({"mcpServers": {"mem0": {"url": "http://h/mcp?token=local"}}}))

    def test_mensagem_junta_atualizacoes_e_avisos(self):
        resultado = at.Resultado(atualizados=["hook a"], adotados=["skill b"], avisos=["aviso"])
        texto = at.mensagem(resultado)
        self.assertIn("hook a", texto)
        self.assertIn("skill b", texto)
        self.assertTrue(texto.endswith("aviso"))
        self.assertIsNone(at.mensagem(at.Resultado()))


if __name__ == "__main__":
    unittest.main()
