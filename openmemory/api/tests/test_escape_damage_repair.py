"""Regressao do dano de escape em caminhos Windows/UNC (memoria a62a97a9, 28/08/2026).

Sem o reparo em ``mem0.memory.technical_content`` estes testes falham: o texto
extraido chega com TAB/NUL/U+0131 no lugar das barras invertidas e o caminho UNC
fica inutilizavel no acervo.

Os literais sao montados a partir de ``B = chr(92)`` de proposito: um teste sobre
barras invertidas nao pode depender de como o proprio arquivo as escapa.
"""

import pytest

from mem0.memory.technical_content import (
    enrich_extracted_memories,
    extract_windows_paths,
    has_escape_damage,
    repair_escape_damage,
)

B = chr(92)

# O caminho real da equipe, exatamente como aparece nos prompts:
# \192.168.3.5\Tarefas\Equipe Financeiro Fiscal\371145
UNC = B + B + "192.168.3.5" + B + "Tarefas" + B + "Equipe Financeiro Fiscal" + B + "371145"

# O que a desserializacao da resposta do LLM gravou de fato no acervo
# (conferido em 01/09/2026 via GET /api/v1/memories/a62a97a9-...).
DAMAGED = (
    "Para testes, o diretorio configurado e "
    + B + "192.168.3.5"
    + chr(9) + "arefas"
    + chr(9) + "ime"
    + chr(0x131) + "quipe Financeiro Fiscal3371145, "
    "contendo os executaveis sysmovs_371145_2.89.24.exe e tributacao.exe."
)

SOURCE = (
    "Para testes o diretorio configurado e "
    + UNC
    + " contendo os executaveis sysmovs_371145_2.89.24.exe e tributacao.exe."
)


def test_fixture_reproduz_o_dano_real():
    """A fixture precisa mesmo carregar os caracteres de controle observados."""
    assert chr(9) in DAMAGED
    assert chr(0x131) in DAMAGED
    assert has_escape_damage(DAMAGED)
    assert not has_escape_damage(SOURCE)


def test_extrai_o_caminho_unc_da_origem():
    assert UNC in extract_windows_paths(SOURCE)


def test_reparo_restaura_o_caminho_unc_verbatim():
    repaired = repair_escape_damage(DAMAGED, SOURCE)
    assert UNC in repaired
    assert not has_escape_damage(repaired)
    assert repaired.startswith("Para testes, o diretorio configurado e ")
    assert "sysmovs_371145_2.89.24.exe" in repaired


def test_reparo_e_no_op_em_texto_sao():
    texto = "A TAREFA #371145 foi liberada em " + UNC + " com a versao 2.89.24."
    assert repair_escape_damage(texto, SOURCE) == texto


def test_sem_caminho_na_origem_o_controle_nao_sobrevive():
    """Pior caso: nao da para reenxertar, mas NUL/TAB nao podem ir para o acervo."""
    damaged = "arquivo latest" + chr(0) + "0000_0000_migration.xml gravado."
    out = repair_escape_damage(damaged, "")
    assert not has_escape_damage(out)
    assert "latest" + B + "0000_0000_migration.xml" in out


def test_pipeline_de_extracao_repara_a_memoria():
    memorias = [{"id": "0", "text": DAMAGED, "attributed_to": "user"}]
    out = enrich_extracted_memories(memorias, SOURCE)
    assert UNC in out[0]["text"]
    assert not any(has_escape_damage(m.get("text") or "") for m in out)


def test_repara_tambem_o_raw_content():
    memorias = [{"id": "0", "text": "Diretorio de testes.", "raw_content": DAMAGED}]
    out = enrich_extracted_memories(memorias, SOURCE)
    assert not has_escape_damage(out[0]["raw_content"])
    assert UNC in out[0]["raw_content"]


@pytest.mark.parametrize(
    "path",
    [
        B + B + "192.168.3.5" + B + "Tarefas" + B + "Equipe Financeiro Fiscal" + B + "340226",
        "C:" + B + "SysmoVs" + B + "dados" + B + "IntegracaoBancaria" + B + "DDA",
        "D:" + B + "dsv-git" + B + "dsv-delphi" + B + "sysmovs" + B + "pas",
    ],
)
def test_reconhece_as_formas_de_caminho_usadas_pela_equipe(path):
    assert path in extract_windows_paths("entregue em " + path + " ontem")
