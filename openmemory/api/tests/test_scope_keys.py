"""Chave de tarefa e procedencia da gravacao (rodada 1 item 3a, rodada 2 item 3).

Sem ``app.utils.scope_keys`` e sem os campos no payload estes testes falham: hoje
o numero da tarefa so existe misturado dentro de ``project``, e os fatos
extraidos de um mesmo texto nao tem nada em comum que os agrupe.
"""

import pytest

from app.utils.scope_keys import infer_task_from_text, normalize_task, resolve_task


class TestNormalizeTask:
    @pytest.mark.parametrize("valor", ["371145", "#371145", " 371145 "])
    def test_aceita_as_formas_usadas_pela_equipe(self, valor):
        assert normalize_task(valor) == "371145"

    @pytest.mark.parametrize("valor", [None, "", "sysmo-s1", "12345", "1234567", "2.89.24"])
    def test_recusa_o_que_nao_e_chave_de_tarefa(self, valor):
        assert normalize_task(valor) is None


class TestInferirDoTexto:
    def test_le_a_forma_explicita(self):
        assert infer_task_from_text("A TAREFA #371145 foi liberada") == "371145"
        assert infer_task_from_text("tarefa 371145 em revisao") == "371145"
        assert infer_task_from_text("Tarefas #371145 e outras") == "371145"

    def test_ignora_numero_solto(self):
        """Falso positivo aqui contamina um filtro que so vale se for confiavel."""
        assert infer_task_from_text("o cliente 105307 pediu 371145 caixas") is None
        assert infer_task_from_text("porta 944311 liberada") is None

    def test_sem_texto_nao_inventa(self):
        assert infer_task_from_text("") is None
        assert infer_task_from_text(None) is None


class TestResolveTask:
    def test_o_explicito_vence_o_texto(self):
        assert resolve_task("372244", "A TAREFA #371145 mudou") == "372244"

    def test_cai_para_o_texto_quando_nao_informado(self):
        assert resolve_task(None, "A TAREFA #371145 mudou") == "371145"

    def test_explicito_invalido_nao_bloqueia_a_inferencia(self):
        assert resolve_task("sysmo-s1", "A TAREFA #371145 mudou") == "371145"

    def test_nada_a_resolver(self):
        assert resolve_task(None, "uma frase qualquer") is None


def test_os_campos_estao_indexados_como_keyword_no_qdrant():
    """Filtro exato sem indice e varredura: o ganho do campo depende disto."""
    from mem0.vector_stores.qdrant import Qdrant

    assert "task" in Qdrant.KEYWORD_INDEX_FIELDS
    assert "ingest_id" in Qdrant.KEYWORD_INDEX_FIELDS
    # project continua sendo a chave de tenant, nao vira campo de tarefa.
    assert Qdrant.TENANT_FIELD == "project"
