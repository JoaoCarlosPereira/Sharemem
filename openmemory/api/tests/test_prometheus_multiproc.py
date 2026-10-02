"""Card 0d30e385: /metrics em modo multiprocesso do prometheus_client.

O ``prometheus_client`` decide o backend de valores (memória x arquivos mmap)
no import, a partir de ``PROMETHEUS_MULTIPROC_DIR``. Para não contaminar o
estado global do processo do pytest, os cenários rodam num interpretador
separado (``subprocess``) que por sua vez cria processos filhos
(``multiprocessing`` com ``spawn``) que observam métricas; o processo pai
então chama o handler real de ``/metrics`` e devolve o corpo da exposição.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests.paths import openmemory_root

API_DIR = Path(__file__).resolve().parents[1]
ROOT = openmemory_root()

# Script executado em subprocesso. Filhos: observam latência de busca, contam
# cache hits, publicam profundidade da fila e incrementam a quarentena. Pai:
# chama o endpoint /metrics real (router path-loaded, sem o __init__ pesado).
_SCENARIO = textwrap.dedent(
    '''
    import asyncio
    import importlib.util
    import json
    import multiprocessing as mp
    import os
    import sys
    from pathlib import Path


    def child(latency, depth):
        from app.utils import metrics as m

        m.SEARCH_LATENCY.observe(latency)
        m.SEARCH_CACHE_HIT.inc()
        m.WRITE_QUEUE_DEPTH.set(depth)
        m.GOVERNANCE_QUARANTINED_CURRENT.inc()
        m.PROJECT_MEMORY_COUNT.labels(project="alpha").set(depth * 10)


    def scrape():
        api_dir = Path(os.environ["SCENARIO_API_DIR"])
        path = api_dir / "app" / "routers" / "ops_metrics.py"
        spec = importlib.util.spec_from_file_location("ops_metrics_under_test", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        response = asyncio.run(mod.metrics())
        return response.body.decode()


    if __name__ == "__main__":
        ctx = mp.get_context("spawn")
        procs = [ctx.Process(target=child, args=(0.03, 7)), ctx.Process(target=child, args=(0.3, 9))]
        for p in procs:
            p.start()
            p.join()
            assert p.exitcode == 0, p.exitcode
        print("__BODY__" + json.dumps({"body": scrape(), "pids": [p.pid for p in procs]}))
    '''
)


def _run_scenario(tmp_path: Path, multiproc_dir: Path | None) -> dict:
    script = tmp_path / "scenario.py"
    script.write_text(_SCENARIO, encoding="utf-8")
    env = dict(os.environ)
    env.pop("PROMETHEUS_MULTIPROC_DIR", None)
    env.pop("prometheus_multiproc_dir", None)
    if multiproc_dir is not None:
        multiproc_dir.mkdir(parents=True, exist_ok=True)
        env["PROMETHEUS_MULTIPROC_DIR"] = str(multiproc_dir)
    env["SCENARIO_API_DIR"] = str(API_DIR)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(API_DIR), env.get("PYTHONPATH", "")) if p)
    env.setdefault("OPENAI_API_KEY", "test-key")
    proc = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(API_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("__BODY__"))
    return json.loads(line[len("__BODY__"):])


def _sample(body: str, name: str) -> float | None:
    for line in body.splitlines():
        if line.startswith("#"):
            continue
        metric, _, value = line.rpartition(" ")
        if metric == name:
            return float(value)
    return None


# -- multiprocesso: agregação entre processos ---------------------------------
@pytest.fixture(scope="module")
def multiproc_result(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("prom_mp")
    return _run_scenario(tmp, tmp / "multiproc"), tmp / "multiproc"


def test_multiproc_aggregates_histogram_across_processes(multiproc_result):
    result, _ = multiproc_result
    body = result["body"]
    # Duas observações em dois processos distintos aparecem somadas.
    assert _sample(body, "mcp_search_latency_seconds_count") == 2.0
    assert _sample(body, "mcp_search_latency_seconds_sum") == pytest.approx(0.33)
    assert _sample(body, 'mcp_search_latency_seconds_bucket{le="0.05"}') == 1.0
    assert _sample(body, 'mcp_search_latency_seconds_bucket{le="0.5"}') == 2.0


def test_multiproc_aggregates_counters(multiproc_result):
    result, _ = multiproc_result
    assert _sample(result["body"], "search_cache_hit_total") == 2.0


def test_multiproc_gauges_have_no_pid_label(multiproc_result):
    """multiprocess_mode explícito: sem série por PID (alertas seguem válidos)."""
    result, _ = multiproc_result
    body = result["body"]
    assert 'pid="' not in body
    # mostrecent: último set vence (segundo filho publicou 90).
    assert _sample(body, 'project_memory_count{project="alpha"}') == 90.0
    # sum: gauge alterado por inc() soma as contribuições dos dois processos.
    assert _sample(body, "governance_quarantined_current") == 2.0


def test_multiproc_live_gauge_dropped_after_mark_process_dead(multiproc_result):
    """livemostrecent: processo encerrado (atexit) some do agregado da fila."""
    result, mp_dir = multiproc_result
    # Os filhos saíram normalmente -> atexit removeu seus gauge_live*.
    leftovers = [p.name for p in mp_dir.glob("gauge_live*_*.db")]
    for pid in result["pids"]:
        assert not any(name.endswith(f"_{pid}.db") for name in leftovers), leftovers
    assert _sample(result["body"], "write_queue_depth") is None
    # Arquivos de counters/histogramas persistem (agregação após o fim do PID).
    assert list(mp_dir.glob("counter_*.db"))
    assert list(mp_dir.glob("histogram_*.db"))


# -- sem a variável: comportamento atual (registry por processo) --------------
def test_without_env_metrics_only_reflect_current_process(tmp_path):
    result = _run_scenario(tmp_path, None)
    body = result["body"]
    # Valores observados nos filhos NÃO aparecem: só o processo que responde.
    assert _sample(body, "mcp_search_latency_seconds_count") == 0.0
    assert _sample(body, "search_cache_hit_total") == 0.0
    # Métricas de processo do registry padrão continuam presentes.
    assert "process_" in body or "python_info" in body
    assert not list(tmp_path.glob("*.db"))


def test_generate_payload_without_env_matches_default_registry(monkeypatch):
    from prometheus_client import generate_latest

    from app.utils import metrics as _metrics  # noqa: F401 - registra no REGISTRY
    from app.utils import prometheus_multiproc as pm

    monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
    monkeypatch.delenv("prometheus_multiproc_dir", raising=False)
    assert pm.multiproc_dir() is None
    payload = pm.generate_metrics_payload()
    assert b"mcp_search_latency_seconds" in payload
    # Mesmo conjunto de séries do registry padrão (valores de gc podem variar).
    def _names(raw: bytes) -> set[bytes]:
        return {ln.split(b" ")[2] for ln in raw.splitlines() if ln.startswith(b"# TYPE ")}

    assert _names(payload) == _names(generate_latest())
    # No-op seguro sem multiproc.
    pm.mark_current_process_dead()


# -- definições: todos os Gauges declaram multiprocess_mode -------------------
def test_every_gauge_declares_multiprocess_mode():
    from prometheus_client import Gauge

    from app.utils import metrics as m

    gauges = {name: obj for name, obj in vars(m).items() if isinstance(obj, Gauge)}
    assert gauges
    for name, gauge in gauges.items():
        assert gauge._multiprocess_mode != "all", f"{name} sem multiprocess_mode explícito"


# -- entrypoint: limpeza do diretório antes dos processos Python --------------
def test_entrypoint_cleans_multiproc_dir_and_execs(tmp_path):
    mp_dir = tmp_path / "mp"
    mp_dir.mkdir()
    (mp_dir / "counter_123.db").write_bytes(b"x")
    (mp_dir / "gauge_livesum_123.db").write_bytes(b"x")
    keep = mp_dir / "keep.txt"
    keep.write_text("ok")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PROMETHEUS_MULTIPROC_DIR": str(mp_dir)}
    proc = subprocess.run(
        ["sh", str(API_DIR / "docker-entrypoint.sh"), "sh", "-c", 'echo "ran:$PROMETHEUS_MULTIPROC_DIR"'],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"ran:{mp_dir}"
    assert not list(mp_dir.glob("*.db"))
    assert keep.exists()


def test_entrypoint_creates_missing_dir_and_noop_without_env(tmp_path):
    mp_dir = tmp_path / "nested" / "mp"
    base_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    proc = subprocess.run(
        ["sh", str(API_DIR / "docker-entrypoint.sh"), "true"],
        env={**base_env, "PROMETHEUS_MULTIPROC_DIR": str(mp_dir)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert mp_dir.is_dir()
    proc = subprocess.run(
        ["sh", str(API_DIR / "docker-entrypoint.sh"), "echo", "plain"],
        env=base_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "plain"


def test_entrypoint_refuses_root_dir():
    proc = subprocess.run(
        ["sh", str(API_DIR / "docker-entrypoint.sh"), "true"],
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PROMETHEUS_MULTIPROC_DIR": "/"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode != 0


# -- deploy: compose/Dockerfile ----------------------------------------------
yaml = pytest.importorskip("yaml")


@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load((ROOT / "docker-compose.scale.yml").read_text(encoding="utf-8"))


def test_compose_api_has_own_tmpfs_multiproc_dir(compose):
    svc = compose["services"]["openmemory-mcp"]
    mp_dir = svc["environment"]["PROMETHEUS_MULTIPROC_DIR"]
    assert mp_dir.startswith("/tmp/")
    assert any(str(entry).split(":")[0] == mp_dir for entry in svc["tmpfs"])
    # Herda o restante do ambiente comum (guarda de exclusão inclusa).
    assert svc["environment"]["MEM0_ALLOW_MEMORY_DELETE"] == "${MEM0_ALLOW_MEMORY_DELETE:-0}"
    # Não é volume nomeado.
    assert all(mp_dir not in str(v) for v in svc.get("volumes", []))


def test_compose_multiproc_dir_not_shared_by_other_services(compose):
    assert "PROMETHEUS_MULTIPROC_DIR" not in compose["x-api-common"]["environment"]
    for name, svc in compose["services"].items():
        if name == "openmemory-mcp":
            continue
        env = svc.get("environment") or {}
        if isinstance(env, dict):
            assert "PROMETHEUS_MULTIPROC_DIR" not in env, name
    assert "tmpfs" not in compose["services"]["mem0_store"]


def test_dockerfile_uses_cleanup_entrypoint():
    dockerfile = (API_DIR / "Dockerfile").read_text(encoding="utf-8")
    assert 'ENTRYPOINT ["sh", "/usr/src/openmemory/docker-entrypoint.sh"]' in dockerfile
