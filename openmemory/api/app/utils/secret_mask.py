"""Mascaramento de segredos em configurações expostas pela API.

Usado por ``/api/v1/config`` para nunca devolver ``api_key``/``password``/tokens
em texto puro, e para que a ida-e-volta (GET → editar → PUT) não grave a
máscara por cima do segredo real.

Regras de detecção (em qualquer nível de dicts **e listas**):

- Chave sensível pelo **nome** (``api_key``, ``password``, ``token``, ``secret``,
  ``*_api_key``, ``connection_string``, headers como ``Authorization``,
  ``X-Api-Key``, ``Cookie``…). ``max_tokens`` e afins **não** casam (o padrão
  exige o nome inteiro ou sufixo ``_<nome>`` / ``-<nome>``). Sob uma chave
  sensível, todas as strings da subárvore (listas/dicts) são mascaradas.
- Qualquer string que seja URL com senha embutida (``scheme://user:pass@host``)
  ou com segredo na query string (``?api_key=``, ``token=``, ``password=``…).
- Referências ``env:VAR`` não são segredo (apontam para variável de ambiente) e
  são devolvidas como estão. Strings vazias também.

Formato da máscara: :data:`MASK_PREFIX` (``****``), seguido dos 4 últimos
caracteres do valor só para segredos longos. :func:`is_masked` é deliberadamente
conservador: **qualquer** string que comece com ``****`` conta como máscara.

A restauração e a trava 422 (:func:`restore_masked_secrets`,
:func:`find_masked_values`) só procuram máscara nas posições em que
:func:`mask_config_secrets` mascararia: sob chave sensível (qualquer string
iniciada com ``****``) ou, fora dela, no formato **exato** da máscara de URL
(``****`` ou ``****`` + 4 chars). Texto livre (ex.:
``openmemory.custom_instructions = "**** atenção ..."``) não bloqueia gravação.

Falso positivo conhecido (aceito de propósito): sob chave sensível, um valor real
que comece com ``****`` é indistinguível da máscara — no PUT/PATCH ele é tratado
como "manter o segredo atual" e, se não houver segredo anterior no mesmo
caminho, a chave é descartada (ou, em lista, a escrita é rejeitada com 422).
Fora de chave sensível, só um valor de exatamente ``****`` / ``****xxxx`` sem URL
com credencial correspondente gera 422. Preferimos isso a correr o risco de
persistir uma máscara por cima do segredo. Para esses valores use ``env:VAR``.
"""

from __future__ import annotations

import copy
import re
from typing import Any, List
from urllib.parse import parse_qsl, urlsplit

MASK_PREFIX = "****"
_REVEAL_SUFFIX_MIN_LEN = 12  # só revela os 4 últimos chars de segredos longos

_SENSITIVE_NAME = (
    # Plural só onde é inequívoco (``tokens`` ficaria ambíguo com ``max_tokens``).
    r"api[_-]?keys?|apikeys?|passwords?|passwd|pwd|secrets?|secret[_-]?keys?|token|"
    r"access[_-]?token|auth[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"access[_-]?key|private[_-]?key|credentials?|connection[_-]?string|"
    r"authorization|proxy[_-]?authorization|cookie|signature|auth"
)
_SENSITIVE_KEY_RE = re.compile(rf"^(?:.*[_-])?(?:{_SENSITIVE_NAME})$", re.IGNORECASE)


def is_sensitive_key(key: Any) -> bool:
    return isinstance(key, str) and bool(_SENSITIVE_KEY_RE.match(key))


def _url_has_secret(value: str) -> bool:
    """URL com senha embutida ou com parâmetro sensível na query string."""
    if "://" not in value:
        return False
    try:
        parts = urlsplit(value)
        if "@" in parts.netloc and parts.password:
            return True
        query = parts.query
    except ValueError:
        return False
    if not query:
        return False
    return any(is_sensitive_key(name) and val for name, val in parse_qsl(query, keep_blank_values=True))


def is_masked(value: Any) -> bool:
    """True se ``value`` parece máscara (começa com ``****``; ver docstring do módulo)."""
    return isinstance(value, str) and value.startswith(MASK_PREFIX)


def mask_secret(value: str) -> str:
    """Mascara um segredo: ``****`` + 4 últimos chars (só se for longo)."""
    if len(value) >= _REVEAL_SUFFIX_MIN_LEN:
        return f"{MASK_PREFIX}{value[-4:]}"
    return MASK_PREFIX


def _is_maskable_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and not value.startswith("env:") and not is_masked(value)


def mask_config_secrets(data: Any, *, _sensitive: bool = False) -> Any:
    """Cópia profunda de ``data`` com todos os segredos mascarados.

    Percorre dicts e listas em qualquer profundidade. ``_sensitive`` indica que
    o nó está sob uma chave sensível (todas as strings da subárvore são segredo).
    """
    if isinstance(data, dict):
        return {key: mask_config_secrets(value, _sensitive=_sensitive or is_sensitive_key(key)) for key, value in data.items()}
    if isinstance(data, (list, tuple)):
        return [mask_config_secrets(item, _sensitive=_sensitive) for item in data]
    if _is_maskable_string(data) and (_sensitive or _url_has_secret(data)):
        return mask_secret(data)
    return copy.deepcopy(data)


def _real_secret(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and not is_masked(value)


def _is_exact_mask(value: Any) -> bool:
    """Formato exato produzido por :func:`mask_secret` (``****`` ou ``****`` + 4)."""
    return is_masked(value) and len(value) in (len(MASK_PREFIX), len(MASK_PREFIX) + 4)


def _is_mask_at(value: Any, sensitive: bool) -> bool:
    """Máscara **nesta posição** — só onde :func:`mask_config_secrets` mascararia.

    - Sob chave sensível (``api_key``, ``password``, headers ``Authorization``…):
      conservador, qualquer string iniciada com ``****`` (ver docstring do módulo).
    - Fora de chave sensível só existem máscaras de URL com credencial/segredo na
      query; a máscara perde o formato de URL, então reconhecemos o formato
      **exato** da máscara (``****`` ou ``****`` + 4 chars). Texto livre como
      ``openmemory.custom_instructions = "**** Importante: ..."`` não é máscara.
    """
    return is_masked(value) if sensitive else _is_exact_mask(value)


def _would_mask(value: Any, sensitive: bool) -> bool:
    """True se :func:`mask_config_secrets` mascararia ``value`` nesta posição."""
    return _is_maskable_string(value) and (sensitive or _url_has_secret(value))


def restore_masked_secrets(incoming: Any, current: Any, *, _sensitive: bool = False) -> Any:
    """Substitui (in-place) valores mascarados de ``incoming`` pelo valor real.

    Só considera máscara nas posições que :func:`mask_config_secrets` mascararia
    (ver :func:`_is_mask_at`). Para cada máscara em ``incoming``, usa o valor do
    mesmo caminho em ``current`` (config persistida antes da escrita): dicts por
    chave, listas por índice (só quando os tamanhos batem — senão não há
    correspondência confiável). Fora de chave sensível o valor atual só é usado
    se ele próprio seria mascarado (URL com credencial). Sem valor real
    correspondente, uma chave sensível de dict é removida; nos demais casos a
    máscara fica e é barrada por :func:`assert_no_masked_secrets` (422). A
    máscara **nunca** é persistida. Retorna ``incoming``.
    """
    if isinstance(incoming, dict):
        current_dict = current if isinstance(current, dict) else {}
        for key in list(incoming.keys()):
            value = incoming[key]
            sensitive = _sensitive or is_sensitive_key(key)
            if _is_mask_at(value, sensitive):
                real = current_dict.get(key)
                if _real_secret(real) and _would_mask(real, sensitive):
                    incoming[key] = real
                elif sensitive:
                    del incoming[key]
            else:
                restore_masked_secrets(value, current_dict.get(key), _sensitive=sensitive)
    elif isinstance(incoming, list):
        same_shape = isinstance(current, list) and len(current) == len(incoming)
        for index, value in enumerate(incoming):
            counterpart = current[index] if same_shape else None
            if _is_mask_at(value, _sensitive):
                if _real_secret(counterpart) and _would_mask(counterpart, _sensitive):
                    incoming[index] = counterpart
            else:
                restore_masked_secrets(value, counterpart, _sensitive=_sensitive)
    return incoming


def find_masked_values(data: Any, path: str = "", *, _sensitive: bool = False) -> List[str]:
    """Caminhos (``a.b[0].c``) das máscaras remanescentes.

    Só nas posições que :func:`mask_config_secrets` mascararia (chave sensível
    em qualquer nível acima, ou formato exato de máscara de URL) — texto livre
    iniciado com ``****`` fora dessas posições não bloqueia a gravação.
    """
    found: List[str] = []
    if isinstance(data, dict):
        for key, value in data.items():
            found.extend(
                find_masked_values(
                    value,
                    f"{path}.{key}" if path else str(key),
                    _sensitive=_sensitive or is_sensitive_key(key),
                )
            )
    elif isinstance(data, (list, tuple)):
        for index, value in enumerate(data):
            found.extend(find_masked_values(value, f"{path}[{index}]", _sensitive=_sensitive))
    elif _is_mask_at(data, _sensitive):
        found.append(path or "<root>")
    return found


class MaskedSecretError(ValueError):
    """Payload ainda contém máscara de segredo após a restauração."""

    def __init__(self, paths: List[str]):
        self.paths = paths
        super().__init__(
            "valor mascarado ('****…') não pode ser gravado em: "
            + ", ".join(paths)
            + ". Informe o segredo real ou uma referência env:VAR."
        )


def assert_no_masked_secrets(data: Any) -> None:
    """Garantia final antes de persistir: levanta se sobrou qualquer máscara."""
    paths = find_masked_values(data)
    if paths:
        raise MaskedSecretError(paths)
