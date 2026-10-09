"""Regressões do instalador para falhas vistas no deploy LAN (Docker/Ollama/UI)."""

import importlib.util
from types import SimpleNamespace

import pytest

from tests.paths import openmemory_root

_ROOT = openmemory_root()
_INSTALL_PATH = _ROOT.parent / "install.py"
_spec = importlib.util.spec_from_file_location("install_hardening_under_test", _INSTALL_PATH)
install = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(install)


@pytest.fixture
def compose_env(tmp_path):
    return tmp_path / ".env"


class TestStoragePreservesExisting:
    def test_yes_sem_data_dir_mantem_qdrant_storage(self, compose_env):
        install.set_env(compose_env, "QDRANT_STORAGE", "/mnt/Dados/memorias/qdrant")
        result = install.configure_storage_scale(
            None, interactive=False, compose_env=compose_env
        )
        assert install.read_env(compose_env, "QDRANT_STORAGE") == "/mnt/Dados/memorias/qdrant"
        assert result == "/mnt/Dados/memorias"

    def test_sem_existente_usa_volume_nomeado(self, compose_env):
        install.configure_storage_scale(None, interactive=False, compose_env=compose_env)
        assert install.read_env(compose_env, "QDRANT_STORAGE") == "mem0_storage"

    def test_volume_nomeado_nao_vira_caminho(self, compose_env):
        """``mem0_storage`` é nome de volume Docker, não caminho — não tem pai."""
        install.set_env(compose_env, "QDRANT_STORAGE", "mem0_storage")
        assert (
            install.configure_storage_scale(
                None, interactive=False, compose_env=compose_env
            )
            is None
        )


class TestStorageParent:
    """O valor gravado no .env é caminho do HOST DE DEPLOY (Linux).

    Derivar o pai com ``pathlib.Path`` ancora o caminho no sistema de quem roda o
    instalador: no Windows, "/mnt/Dados/memorias/qdrant" virava
    "D:\\mnt\\Dados\\memorias" — letra de unidade inventada e barras trocadas.
    """

    def test_caminho_posix_preserva_barras_e_nao_ganha_unidade(self):
        assert install._storage_parent("/mnt/Dados/memorias/qdrant") == "/mnt/Dados/memorias"

    def test_nao_depende_do_so_de_quem_roda_o_instalador(self):
        resultado = install._storage_parent("/srv/mem0/qdrant")
        assert resultado == "/srv/mem0"
        assert "\\" not in resultado
        assert ":" not in resultado

    def test_caminho_raiz(self):
        assert install._storage_parent("/qdrant") == "/"

    def test_valor_com_barra_invertida_e_normalizado(self):
        assert install._storage_parent("C:\\dados\\qdrant") == "C:/dados"


class TestOllamaHostDetection:
    def test_inference_uses_host_ollama_from_env(self, compose_env):
        install.set_env(compose_env, "LLM_PROVIDER", "ollama")
        install.set_env(compose_env, "OLLAMA_LLM_URL", "http://host.docker.internal:11434")
        assert install.inference_uses_host_ollama(compose_env) is True

    def test_inference_ignora_api_remota(self, compose_env):
        install.set_env(compose_env, "LLM_PROVIDER", "openai")
        install.set_env(compose_env, "LLM_BASE_URL", "https://api.openai.com/v1")
        assert install.inference_uses_host_ollama(compose_env) is False

    def test_ollama_listening_all_interfaces(self, monkeypatch):
        monkeypatch.setattr(install.shutil, "which", lambda cmd: "/usr/bin/ss" if cmd == "ss" else None)

        class _R:
            stdout = "LISTEN 0 4096 *:11434 *:*\n"

        monkeypatch.setattr(
            install.subprocess, "run",
            lambda *a, **k: _R(),
        )
        assert install.ollama_listening_for_docker() is True

    def test_ollama_listening_localhost_only(self, monkeypatch):
        monkeypatch.setattr(install.shutil, "which", lambda cmd: "/usr/bin/ss" if cmd == "ss" else None)

        class _R:
            stdout = "LISTEN 0 4096 127.0.0.1:11434 0.0.0.0:*\n"

        monkeypatch.setattr(
            install.subprocess, "run",
            lambda *a, **k: _R(),
        )
        assert install.ollama_listening_for_docker() is False


class TestHealthAndUiHelpers:
    def test_parse_health_and_memory_client(self):
        detail = (
            'HTTP 200: {"status":"degraded","checks":{"memory_client":'
            '{"status":"degraded","error":"Failed to connect to Ollama"}}}'
        )
        assert install.memory_client_status(detail) == "degraded"
        assert install.parse_health_payload(detail)["status"] == "degraded"

    def test_ensure_docker_access_ok_quando_usable(self, monkeypatch):
        monkeypatch.setattr(install, "docker_usable", lambda: True)
        args = SimpleNamespace(yes=True)
        install.ensure_docker_access(args, interactive=False)  # não deve die


class TestBuildxHardening:
    """Buildx é uma otimização opcional; o builder clássico deve funcionar."""

    def test_agentregistry_dockerfile_supports_classic_builder(self):
        dockerfile = (
            _ROOT.parent / "integrations" / "agentregistry" / "docker" / "Dockerfile.mem0"
        ).read_text(encoding="utf-8")
        assert "BUILDPLATFORM" not in dockerfile
        assert "FROM --platform=" not in dockerfile
        assert "${TARGETARCH:-$(go env GOARCH)}" in dockerfile

    def test_have_buildx_true_when_run_succeeds(self, monkeypatch):
        class Result:
            returncode = 0

        monkeypatch.setattr(install, "run", lambda *a, **k: Result())
        assert install.have_buildx() is True

    def test_have_buildx_false_when_run_fails(self, monkeypatch):
        class Result:
            returncode = 1

        monkeypatch.setattr(install, "run", lambda *a, **k: Result())
        assert install.have_buildx() is False

    def test_install_buildx_falls_back_to_ubuntu_package(self, monkeypatch):
        calls = []

        class Result:
            def __init__(self, returncode):
                self.returncode = returncode

        def fake_run(command, **kwargs):
            calls.append(command)
            if command[-1] == "docker-buildx-plugin":
                return Result(100)
            return Result(0)

        monkeypatch.setattr(install.sys, "platform", "linux")
        monkeypatch.setattr(install.shutil, "which", lambda exe: f"/usr/bin/{exe}" if exe == "apt-get" else None)
        monkeypatch.setattr(install, "_maybe_sudo", lambda command: command)
        monkeypatch.setattr(install, "run", fake_run)

        assert install.install_buildx_plugin() is True
        assert ["apt-get", "install", "-y", "docker-buildx-plugin"] in calls
        assert ["apt-get", "install", "-y", "docker-buildx"] in calls

    def test_ensure_buildx_installed_noop_when_present(self, monkeypatch):
        monkeypatch.setattr(install, "have_buildx", lambda: True)
        monkeypatch.setattr(
            install,
            "install_buildx_plugin",
            lambda: pytest.fail("não deveria tentar instalar; buildx já presente"),
        )
        args = SimpleNamespace(yes=True)
        install.ensure_buildx_installed(args, interactive=False)  # não deve die

    def test_ensure_buildx_installed_installs_with_yes(self, monkeypatch):
        state = {"installed": False}
        monkeypatch.setattr(install, "have_buildx", lambda: state["installed"])

        def fake_install():
            state["installed"] = True
            return True

        monkeypatch.setattr(install, "install_buildx_plugin", fake_install)
        args = SimpleNamespace(yes=True)
        install.ensure_buildx_installed(args, interactive=False)  # não deve die
        assert state["installed"] is True

    def test_ensure_buildx_installed_never_dies_without_consent(self, monkeypatch):
        """Diferente de Docker/Compose, buildx ausente é best-effort (warn, não die)."""
        monkeypatch.setattr(install, "have_buildx", lambda: False)
        args = SimpleNamespace(yes=False)
        install.ensure_buildx_installed(args, interactive=False)  # não deve die


class TestEntrypointAndRequirements:
    def test_entrypoint_nao_faz_sed_bare_de_identifiers(self):
        src = (_ROOT / "ui" / "entrypoint.sh").read_text(encoding="utf-8")
        assert 's|"${key}"|"${esc}"|g' in src or 's|\\"${key}\\"|\\"${esc}\\"|g' in src
        assert "process.env.0" in src or "bare" in src.lower() or "NEVER sed bare" in src
        # Não deve restar o padrão antigo que quebrava AUTH_UI_REQUIRED=0.
        assert 's|${key}|${value}|g' not in src

    def test_requirements_pin_fastapi_abaixo_de_0_140_5(self):
        req = (_ROOT / "api" / "requirements.txt").read_text(encoding="utf-8")
        assert "fastapi>=0.68.0,<0.140.5" in req


class TestEnsureUpdateOpsEnvSocketOrigins:
    """Regressão: SOCKET_ALLOWED_ORIGINS vazio caía no default hardcoded do
    compose (IP de outro host) — Socket.IO do PLANKA rejeitava o origin real
    da LAN e o board ficava "carregando para sempre" (CORS bloqueado)."""

    def test_deriva_origins_do_ip_lan_e_planka_base_url(self, compose_env, monkeypatch):
        monkeypatch.setattr(install, "detect_lan_ip", lambda: "192.168.2.118")
        install.set_env(compose_env, "PROXY_PORT", "8765")
        install.set_env(compose_env, "PLANKA_BASE_URL", "http://192.168.2.118:8765/planka")

        install.ensure_update_ops_env(compose_env)

        origins = (install.read_env(compose_env, "SOCKET_ALLOWED_ORIGINS") or "").split(",")
        assert "http://localhost:3000" in origins
        assert "http://127.0.0.1:3000" in origins
        assert "http://192.168.2.118:3000" in origins
        assert "http://192.168.2.118:8765" in origins

    def test_inclui_origin_https_do_nextauth_url(self, compose_env, monkeypatch):
        monkeypatch.setattr(install, "detect_lan_ip", lambda: None)
        install.set_env(compose_env, "NEXTAUTH_URL", "https://memorias.sysmo.com.br")

        install.ensure_update_ops_env(compose_env)

        origins = (install.read_env(compose_env, "SOCKET_ALLOWED_ORIGINS") or "").split(",")
        assert "https://memorias.sysmo.com.br" in origins

    def test_nao_sobrescreve_valor_ja_configurado(self, compose_env, monkeypatch):
        monkeypatch.setattr(install, "detect_lan_ip", lambda: "192.168.2.118")
        install.set_env(compose_env, "SOCKET_ALLOWED_ORIGINS", "https://custom.example.com")

        install.ensure_update_ops_env(compose_env)

        assert (
            install.read_env(compose_env, "SOCKET_ALLOWED_ORIGINS")
            == "https://custom.example.com"
        )


class TestInstallerWiringHardening:
    def test_run_production_valida_ui_e_ollama(self):
        src = _INSTALL_PATH.read_text(encoding="utf-8")
        assert "ensure_ollama_reachable_from_docker(" in src
        assert "wait_for_ui(" in src
        assert "ensure_docker_access(" in src
        assert 'set_env(compose_env, "AUTH_UI_REQUIRED", "0")' in src

    def test_agentregistry_sidecar_is_started_and_healthchecked(self):
        src = _INSTALL_PATH.read_text(encoding="utf-8")
        assert "def wait_for_agentregistry" in src
        # Exatamente uma chamada por fluxo (--update e instalação nova); uma
        # segunda chamada órfã (bug de merge entre duas correções da mesma
        # lacuna) reconstruiria os sidecars duas vezes sem necessidade.
        assert src.count("ensure_sidecars_after_update(dc") == 3  # def + 2 chamadas
        assert "http://127.0.0.1:8080/v0/ping" in src

    def test_buildx_checado_nos_dois_fluxos_de_prerequisito(self):
        src = _INSTALL_PATH.read_text(encoding="utf-8")
        assert "def ensure_buildx_installed" in src
        assert src.count("ensure_buildx_installed(args, prereq_interactive)") == 2

    def test_wait_for_agentregistry_probes_inside_container(self):
        calls = []

        class Result:
            returncode = 0

        def dc(*args, **kwargs):
            calls.append((args, kwargs))
            return Result()

        assert install.wait_for_agentregistry(dc, timeout=1, interval=0) is True
        assert calls[0][0] == (
            "exec",
            "-T",
            "agentregistry",
            "wget",
            "-qO-",
            "http://127.0.0.1:8080/v0/ping",
        )
        assert calls[0][1]["stdout"] is install.subprocess.DEVNULL


class TestInstallerUpdatePull:
    def test_git_pull_reports_failure(self, monkeypatch):
        calls = []

        class Result:
            returncode = 1

        monkeypatch.setattr(install, "shutil", SimpleNamespace(which=lambda _: "/usr/bin/git"))
        monkeypatch.setattr(install, "run", lambda *args, **kwargs: calls.append((args, kwargs)) or Result())
        monkeypatch.setattr(install, "ROOT", _ROOT.parent)

        assert install.git_pull() is False
        assert calls[0][0][0] == ["git", "pull", "--ff-only"]

    def test_git_pull_success_is_reported(self, monkeypatch):
        class Result:
            returncode = 0

        monkeypatch.setattr(install, "shutil", SimpleNamespace(which=lambda _: "/usr/bin/git"))
        monkeypatch.setattr(install, "run", lambda *args, **kwargs: Result())
        monkeypatch.setattr(install, "ROOT", _ROOT.parent)

        assert install.git_pull() is True

    def test_update_requires_explicit_no_pull_after_pull_failure(self):
        src = _INSTALL_PATH.read_text(encoding="utf-8")
        assert "if not git_pull():" in src
        assert "use --no-pull" in src


class TestStoreKindsAfterUpdate:
    """A Store só está pronta quando o Registry serve todas as coleções."""

    @staticmethod
    def _dc(failures):
        """docker compose falso: falha nas coleções listadas em ``failures``."""
        calls = []

        def dc(*args, **kwargs):
            calls.append(args)
            url = next((arg for arg in args if str(arg).startswith("http")), "")
            rc = 1 if any(f"/v0/{kind}?" in url for kind in failures) else 0
            return SimpleNamespace(returncode=rc)

        dc.calls = calls
        return dc

    def test_todas_as_colecoes_no_ar(self):
        dc = self._dc(failures=())
        assert install.wait_for_store_kinds(dc, timeout=1) is True

    def test_kind_novo_ausente_avisa_em_vez_de_passar(self):
        """Imagem antiga do agentregistry passa no ping mas não serve hooks."""
        dc = self._dc(failures=("hooks",))
        assert install.wait_for_store_kinds(dc, timeout=1, interval=0) is False

    def test_hooks_fazem_parte_do_check_padrao(self):
        dc = self._dc(failures=())
        install.wait_for_store_kinds(dc, timeout=1)
        probed = [str(arg) for call in dc.calls for arg in call if str(arg).startswith("http")]
        assert any("/v0/hooks?" in url for url in probed)
        assert any("/v0/skills?" in url for url in probed)
