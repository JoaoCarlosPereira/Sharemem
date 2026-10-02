"""``scripts/bench-rerank-latency.py`` rodado POR CAMINHO (card 80110071, D1 do QA).

O runbook manda rodar ``python /usr/src/openmemory/scripts/bench-rerank-latency.py``
dentro do container. Rodar por caminho põe só a pasta do script em
``sys.path[0]``; sem o bootstrap o script quebrava com
``ModuleNotFoundError: No module named 'app'``. Estes testes reproduzem os dois
layouts (imagem e checkout) em ``tmp_path``, sem ``PYTHONPATH``, e um controle
sem ``app/`` que prova que o teste detectaria a regressão. Nada toca rede,
Qdrant, banco ou torch: só ``--help`` e ``--check-imports``.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.paths import openmemory_root

_SCRIPT = openmemory_root() / "scripts" / "bench-rerank-latency.py"
_APP = Path(__file__).resolve().parents[1] / "app"
# Fork mem0 da raiz do checkout (na imagem vem por PYTHONPATH=/usr/src).
_MEM0 = openmemory_root().parent / "mem0"


def _layout(base: Path, api_subdir: bool, with_app: bool = True) -> Path:
    root = base / "openmemory"
    (root / "scripts").mkdir(parents=True)
    # Cópia (não symlink): o script usa Path(__file__).resolve().
    script = root / "scripts" / _SCRIPT.name
    shutil.copy(_SCRIPT, script)
    if with_app:
        api = root / "api" if api_subdir else root
        api.mkdir(exist_ok=True)
        (api / "app").symlink_to(_APP, target_is_directory=True)
    if _MEM0.is_dir():
        # Raiz do "repo" ao lado de openmemory/: o bootstrap a acrescenta ao sys.path.
        (base / "mem0").symlink_to(_MEM0, target_is_directory=True)
    return script


def _run(script: Path, *args: str, cwd: Path, tmp_path: Path):
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.setdefault("OPENAI_API_KEY", "test-key")
    # Nunca o openmemory.db do worktree nem o Postgres da LAN.
    env["DATABASE_URL"] = f"sqlite:///{tmp_path / 'bench.db'}"
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.fixture(autouse=True)
def _need_script():
    if not _SCRIPT.is_file():
        pytest.skip("scripts/ ausente")


def test_help_does_not_import_the_app(tmp_path):
    # Sem app/ algum: se --help importasse o app (ou torch) falharia aqui.
    script = _layout(tmp_path / "layout", api_subdir=False, with_app=False)
    r = _run(script, "--help", cwd=tmp_path, tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "--check-imports" in r.stdout
    assert "Traceback" not in r.stderr


@pytest.mark.parametrize("api_subdir", [False, True], ids=["imagem", "repo"])
def test_por_caminho_sem_module_not_found(tmp_path, api_subdir):
    # Imagem: /usr/src/openmemory/{app,scripts}; repo: openmemory/{api/app,scripts}.
    script = _layout(tmp_path / "layout", api_subdir)
    elsewhere = tmp_path / "cwd"
    elsewhere.mkdir()

    r = _run(script, "--help", cwd=elsewhere, tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "ModuleNotFoundError" not in r.stderr

    r = _run(script, "--check-imports", cwd=elsewhere, tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "ModuleNotFoundError" not in r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["imports"] == "ok"
    assert out["torch_loaded"] is False, "o bench não pode importar torch sem o provider"
    assert not (elsewhere / "openmemory.db").exists()


def test_controle_sem_app_da_module_not_found(tmp_path):
    """Controle: sem ``app/`` acessível o teste acima detectaria a regressão."""
    script = _layout(tmp_path / "layout", api_subdir=False, with_app=False)
    r = _run(script, "--check-imports", cwd=tmp_path, tmp_path=tmp_path)
    assert r.returncode != 0
    assert "ModuleNotFoundError" in r.stderr
