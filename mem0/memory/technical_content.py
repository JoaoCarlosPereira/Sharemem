"""Detection and preservation of technical content during memory extraction."""

from __future__ import annotations

import re
from typing import Any

RAW_CONTENT_MARKER = "--- Original technical content ---"

# Signals that input likely contains structured technical material.
_TECHNICAL_SIGNAL_PATTERNS = [
    re.compile(r"```[\s\S]*?```", re.MULTILINE),
    re.compile(
        r"(?:^|\n)\s*(?:sudo\s+)?(?:docker|docker-compose|kubectl|npm|pnpm|pip|python3?|bash|sh|curl|wget|git|make|cmake|llama(?:-server|-cli)?)\b[^\n]{20,}",
        re.IGNORECASE | re.MULTILINE,
    ),
    re.compile(
        r"\b(?:SELECT|INSERT|UPDATE|DELETE|CREATE\s+TABLE|ALTER\s+TABLE|DROP\s+TABLE)\b[\s\S]{10,}?;",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:Traceback|Exception|Error|panic:|FATAL|ERROR)\b[^\n]{0,200}", re.IGNORECASE),
    re.compile(r"(?:^|\n)\d{4}[-/]\d{2}[-/]\d{2}[T\s]\d{2}:\d{2}:\d{2}[^\n]*(?:ERROR|WARN|FATAL)", re.MULTILINE),
    re.compile(r"(?:^|\n)\s*(?:version|apiVersion|kind|services|volumes|environment):\s", re.MULTILINE | re.IGNORECASE),
    re.compile(r"(?:^|\n)\s*[\w.-]+\s*:\s*[\w\"'/\\.-]+", re.MULTILINE),
    re.compile(
        r"\b(?:procedure|function|unit|uses|begin|end\.|import\s+\w+|class\s+\w+|def\s+\w+|const\s+\w+)\b",
        re.IGNORECASE,
    ),
    re.compile(r"(?:/[\w.-]+){2,}"),
    re.compile(r"--[\w-]+(?:\s+|=)[^\s\n]+"),
    re.compile(r"\b[A-Z_]{2,}_[A-Z0-9_]+\s*="),
    re.compile(r"\{[^{}]{20,}\}"),
    re.compile(r"\[[^\[\]]{20,}\]"),
]

_VAGUE_TECHNICAL_PATTERNS = [
    re.compile(
        r"user (?:sent|shared|provided|submitted|uploaded|pasted|attached) (?:a |an )?"
        r"(?:script|code|command|config(?:uration)?|file|log|snippet|error)",
        re.IGNORECASE,
    ),
    re.compile(r"user (?:works with|uses|configured|set up) (?:code|scripts?|a service)", re.IGNORECASE),
    re.compile(r"user (?:had|encountered|experienced|reported) (?:an? )?error\b", re.IGNORECASE),
    re.compile(r"user (?:ran|executed|used) (?:a |an )?(?:command|script)\b", re.IGNORECASE),
]

_FENCE_PATTERN = re.compile(r"```(?:[\w+-]*)?\n?([\s\S]*?)```", re.MULTILINE)


def has_technical_content(text: str) -> bool:
    """Return True when the text likely contains structured technical material."""
    if not text or not text.strip():
        return False
    if len(text.strip()) < 20:
        return False
    hits = sum(1 for pattern in _TECHNICAL_SIGNAL_PATTERNS if pattern.search(text))
    return hits >= 1


def extract_technical_segments(text: str) -> list[dict[str, str]]:
    """Extract technical blocks from source text for preservation checks."""
    if not text:
        return []

    segments: list[dict[str, str]] = []
    seen: set[str] = set()

    def _add(content: str, segment_type: str) -> None:
        cleaned = content.strip()
        if len(cleaned) < 15:
            return
        for existing in segments:
            if cleaned in existing["content"]:
                return
        segments[:] = [seg for seg in segments if seg["content"] not in cleaned]
        key = cleaned[:200]
        if key in seen:
            return
        seen.add(key)
        segments.append({"type": segment_type, "content": cleaned})

    for match in _FENCE_PATTERN.finditer(text):
        _add(match.group(1), "code_fence")

    for match in re.finditer(
        r"(?:^|\n)\s*((?:sudo\s+)?(?:docker|docker-compose|kubectl|npm|pnpm|pip|python3?|bash|sh|curl|wget|git|make|cmake|llama(?:-server|-cli)?)\b[^\n]{20,})",
        text,
        re.IGNORECASE,
    ):
        _add(match.group(1), "shell_command")

    for match in re.finditer(
        r"((?:procedure|function)\b[\s\S]+?\bend\s*;)",
        text,
        re.IGNORECASE,
    ):
        _add(match.group(1), "code_block")

    for match in re.finditer(
        r"((?:SELECT|INSERT|UPDATE|DELETE|CREATE\s+TABLE|ALTER\s+TABLE)[^;]+;(?:\s*\n\s*(?:ERROR|Error|Exception)[^\n]*)?)",
        text,
        re.IGNORECASE,
    ):
        _add(match.group(1).strip(), "sql")

    for match in re.finditer(
        r"((?:\d{4}[-/]\d{2}[-/]\d{2}[T\s]\d{2}:\d{2}:\d{2}[^\n]*(?:ERROR|WARN|FATAL)[^\n]*)|"
        r"(?:Traceback|Exception|Error|panic:|FATAL)[\s\S]{20,}?)(?=\n\n|\Z)",
        text,
        re.IGNORECASE,
    ):
        _add(match.group(0), "error_log")

    if not segments and has_technical_content(text):
        _add(text.strip(), "technical_text")

    return segments


def is_vague_technical_summary(text: str) -> bool:
    """Detect overly generic memories that drop technical detail."""
    if not text:
        return False
    if RAW_CONTENT_MARKER in text:
        return False
    normalized = " ".join(text.split())
    if any(pattern.search(normalized) for pattern in _VAGUE_TECHNICAL_PATTERNS):
        return True
    if has_technical_content(text):
        return False
    if len(normalized.split()) <= 12 and re.search(
        r"\b(?:script|command|code|config|log|error|sql|yaml|json|docker)\b",
        normalized,
        re.IGNORECASE,
    ):
        return True
    return False


_GENERIC_PRESERVATION_TOKENS = frozenset(
    {
        "error",
        "warn",
        "fatal",
        "select",
        "insert",
        "update",
        "delete",
        "create",
        "alter",
        "drop",
        "exception",
        "traceback",
    }
)


def _extract_significant_substrings(content: str) -> list[str]:
    """Return distinctive substrings that must appear for content to count as preserved."""
    significant: list[str] = []
    for match in re.finditer(r"--[\w-]+(?:=\S+)?", content):
        significant.append(match.group(0))
    for match in re.finditer(r"(?:/[\w.-]+){2,}", content):
        significant.append(match.group(0))
    for match in re.finditer(
        r"\b[\w.-]+\.(?:py|js|ts|sql|yml|yaml|json|xml|env|sh|pas|dpk|gguf)\b",
        content,
        re.IGNORECASE,
    ):
        significant.append(match.group(0))
    for match in re.finditer(r"https?://[^\s]+", content):
        significant.append(match.group(0))
    for match in re.finditer(r"\b(?:procedure|function)\s+(\w+)", content, re.IGNORECASE):
        significant.append(match.group(1))
    for match in re.finditer(r"'[^']{8,}'|\"[^\"]{8,}\"", content):
        significant.append(match.group(0))
    for match in re.finditer(r"\b[A-Za-z_][\w.-]{7,}\b", content):
        token = match.group(0)
        if token.lower() not in _GENERIC_PRESERVATION_TOKENS:
            significant.append(token)
    return significant


def _extract_preservation_tokens(content: str) -> set[str]:
    """Collect distinctive tokens that should survive extraction."""
    tokens: set[str] = set()
    for substring in _extract_significant_substrings(content):
        tokens.add(substring)
    for match in re.finditer(r"\b[A-Z_][A-Z0-9_]{2,}\b", content):
        if match.group(0).lower() not in _GENERIC_PRESERVATION_TOKENS:
            tokens.add(match.group(0))
    return {token for token in tokens if len(token) >= 3}


def _looks_like_code_segment(content: str, segment_type: str | None = None) -> bool:
    if segment_type in {"code_fence", "code_block"}:
        return True
    return bool(
        re.search(
            r"\b(?:def|class|function|procedure|import|return|begin|end)\b|[{}();]",
            content,
            re.IGNORECASE,
        )
    )


def _code_segment_preserved(segment_content: str, combined_memory_text: str) -> bool:
    compact_corpus = "".join(combined_memory_text.split()).lower()
    compact_seg = "".join(segment_content.split()).lower()
    if compact_seg in compact_corpus:
        return True
    if len(compact_seg) <= 40:
        return compact_seg in compact_corpus
    for size in (60, 50, 40, 30):
        if len(compact_seg) < size:
            continue
        step = max(1, size // 4)
        for start in range(0, len(compact_seg) - size + 1, step):
            if compact_seg[start : start + size] in compact_corpus:
                return True
    return False


def segment_preserved(
    segment_content: str,
    combined_memory_text: str,
    *,
    segment_type: str | None = None,
) -> bool:
    """Return True when distinctive technical tokens from segment appear in memories."""
    if not segment_content:
        return True
    if segment_content.strip() in combined_memory_text:
        return True
    if _looks_like_code_segment(segment_content, segment_type):
        return _code_segment_preserved(segment_content, combined_memory_text)

    significant = _extract_significant_substrings(segment_content)
    if significant:
        combined_lower = combined_memory_text.lower()
        preserved = sum(1 for item in significant if item.lower() in combined_lower)
        threshold = max(1, min(3, len(significant) // 2))
        return preserved >= threshold

    tokens = _extract_preservation_tokens(segment_content)
    if not tokens:
        return segment_content[:80] in combined_memory_text

    combined_lower = combined_memory_text.lower()
    preserved = sum(1 for token in tokens if token.lower() in combined_lower)
    threshold = max(1, min(3, len(tokens) // 2))
    return preserved >= threshold


_BS = chr(92)  # barra invertida, para nao poluir os literais abaixo

# --------------------------------------------------------------------------- #
# Reparo de dano de escape (caminhos Windows/UNC)
# --------------------------------------------------------------------------- #
# O extrator e um LLM e devolve as memorias dentro de uma string JSON. Quando o
# texto de origem traz um caminho Windows, o modelo emite as barras invertidas
# sem duplicar e a desserializacao as consome como sequencias de escape. Medido
# na memoria a62a97a9-66e6-4ddc-b7bc-e471c168d12d (28/08/2026): o caminho de
# rede da equipe foi gravado com TAB no lugar de "\T", U+0131 no lugar de "\E"
# e NUL no lugar de "\0" - caminho UNC inutilizavel.
#
# Caracteres de controle C0 nunca sao legitimos numa frase extraida, entao
# servem de assinatura do dano. Quando o texto de origem ainda tem o caminho
# integro, reenxertamos o caminho verbatim; o que sobrar de controle vira uma
# barra invertida entre caracteres colados (a origem quase certa) ou e removido.

# TAB incluso de proposito: e o produto mais comum do dano. Quebra de linha e
# retorno de carro ficam de fora - sao separadores legitimos.
_CONTROL_CHARS = re.compile(r"[\x00-\x09\x0b\x0c\x0e-\x1f]")

# Caminho UNC (dupla barra + host + share) ou com letra de unidade (C:).
# Segmentos intermediarios aceitam espaco - os shares da equipe se chamam
# "Equipe Financeiro Fiscal" - porque sao delimitados pela barra seguinte. O
# segmento final nao aceita, senao o caminho engoliria o resto da frase.
_PATH_SEG = r"[^\s\\/:*?\"<>|]+"
_PATH_SEG_SP = _PATH_SEG + r"(?:[ ]" + _PATH_SEG + r")*"
_WINDOWS_PATH = re.compile(
    r"(?:[A-Za-z]:\\|\\{1,2})"
    + r"(?:" + _PATH_SEG_SP + r"\\)+"
    + _PATH_SEG
)


def has_escape_damage(text: str) -> bool:
    """True quando o texto carrega caracteres de controle de escape mal desfeito."""
    return bool(text) and bool(_CONTROL_CHARS.search(text))


def extract_windows_paths(text: str) -> list[str]:
    """Caminhos Windows/UNC presentes no texto, do mais longo para o mais curto."""
    if not text:
        return []
    found = {m.group(0).rstrip(".,;:") for m in _WINDOWS_PATH.finditer(text)}
    return sorted(found, key=len, reverse=True)


def _damaged_span(text: str, path: str):
    """Localiza no texto danificado o trecho que corresponde a ``path``."""
    segments = [seg for seg in path.replace("/", _BS).split(_BS) if seg]
    if not segments:
        return None
    head, tail = segments[0], segments[-1]
    i = text.find(head)
    if i < 0:
        return None
    start = i
    while start > 0 and text[start - 1] == _BS:
        start -= 1
    j = text.find(tail, i + len(head))
    if j >= 0:
        end = j + len(tail)
    else:
        # Cauda perdida no dano: para no primeiro delimitador de frase.
        m = re.search(r"[,;\n]", text[i:])
        end = i + m.start() if m else len(text)
    if not _CONTROL_CHARS.search(text[start:end]):
        return None
    return start, end


def _collapse_leftover_controls(text: str) -> str:
    """Controle sobrevivente vira barra invertida entre caracteres colados."""

    def _sub(m):
        pos = m.start()
        before = text[pos - 1] if pos else ""
        after = text[pos + 1] if pos + 1 < len(text) else ""
        if before and after and not before.isspace() and not after.isspace():
            return _BS
        return ""

    return _CONTROL_CHARS.sub(_sub, text)


def repair_escape_damage(text: str, source_text: str = "") -> str:
    """Reconstroi caminhos destruidos por desescape indevido durante a extracao.

    Sem dano, devolve o texto intocado - o caminho normal nao paga nada.
    """
    if not has_escape_damage(text):
        return text
    repaired = text
    for path in extract_windows_paths(source_text or ""):
        span = _damaged_span(repaired, path)
        if span is None:
            continue
        start, end = span
        repaired = repaired[:start] + path + repaired[end:]
        if not has_escape_damage(repaired):
            break
    return _collapse_leftover_controls(repaired)


def format_memory_with_raw(interpreted: str, raw_content: str) -> str:
    """Combine interpreted summary with verbatim technical content."""
    interpreted = (interpreted or "").strip()
    raw_content = (raw_content or "").strip()
    if not raw_content:
        return interpreted
    if raw_content in interpreted:
        return interpreted
    if not interpreted:
        return f"{RAW_CONTENT_MARKER}\n{raw_content}"
    return f"{interpreted}\n\n{RAW_CONTENT_MARKER}\n{raw_content}"


def build_technical_preservation_instructions() -> str:
    """Custom instructions injected when technical content is detected."""
    return (
        "TECHNICAL CONTENT DETECTED in New Messages. Mandatory rules:\n"
        "1. Preserve code, scripts, commands, SQL, JSON/YAML/XML, logs, stack traces, and configs verbatim.\n"
        "2. Include an optional raw_content field with the original technical block unchanged.\n"
        "3. The text field may explain context, but must NOT replace raw_content.\n"
        "4. When in doubt between summarizing and preserving, preserve.\n"
        "5. Never produce vague memories like 'User sent a script' — include names, flags, paths, and parameters.\n"
        "6. Do not rewrite, fix, translate, or sanitize technical content.\n"
        "7. Mark inferred details explicitly; never invent missing technical values."
    )


def _combined_memory_corpus(memories: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for mem in memories:
        parts.append(mem.get("text", "") or "")
        parts.append(mem.get("raw_content", "") or "")
    return "\n".join(parts)


def _summary_related_to_segment(summary: str, segment_content: str) -> bool:
    """True when a non-vague summary already shares distinctive tokens with the segment."""
    if not summary or not segment_content:
        return False
    if segment_preserved(segment_content, summary):
        return True
    significant = _extract_significant_substrings(segment_content)
    if not significant:
        return False
    summary_lower = summary.lower()
    hits = sum(1 for item in significant if item.lower() in summary_lower)
    return hits >= max(1, min(2, len(significant) // 3))


def enrich_extracted_memories(
    extracted_memories: list[dict[str, Any]] | None,
    source_text: str,
) -> list[dict[str, Any]]:
    """Augment LLM extractions so technical source content is not lost.

    Each technical segment is attached at most once. Unrelated narrative
    summaries (e.g. bled from another project's Last-k history) must not receive
    grafted ``--- Original technical content ---`` blocks from the current
    source — those segments become standalone technical_artifact memories.
    """
    memories = list(extracted_memories or [])

    # Reparo de escape antes de qualquer analise: um caminho UNC destruido pela
    # desserializacao da resposta do LLM nao pode ser gravado nem casado com os
    # segmentos tecnicos da origem. Roda sempre, inclusive quando a origem nao e
    # "tecnica" pelos padroes acima - o dano independe disso.
    for mem in memories:
        for field in ("text", "raw_content"):
            value = mem.get(field)
            if isinstance(value, str) and has_escape_damage(value):
                mem[field] = repair_escape_damage(value, source_text)

    if not source_text or not has_technical_content(source_text):
        return memories

    segments = extract_technical_segments(source_text)
    if not segments:
        return memories

    used_segment_indexes: set[int] = set()

    for mem in memories:
        raw = (mem.get("raw_content") or "").strip()
        text = (mem.get("text") or "").strip()
        if raw:
            mem["text"] = format_memory_with_raw(text, raw)
            mem.setdefault("memory_type", "technical")
            for idx, segment in enumerate(segments):
                if idx in used_segment_indexes:
                    continue
                if segment_preserved(
                    segment["content"],
                    f"{text}\n{raw}",
                    segment_type=segment.get("type"),
                ):
                    used_segment_indexes.add(idx)
            continue

        # Mark segments already preserved in this summary as used.
        for idx, segment in enumerate(segments):
            if idx in used_segment_indexes:
                continue
            if segment_preserved(
                segment["content"],
                text,
                segment_type=segment.get("type"),
            ):
                used_segment_indexes.add(idx)

        # Only graft raw onto vague summaries, or summaries clearly about the
        # same segment. Never pair an unrelated narrative with technical raw.
        attach_idx = None
        for idx, segment in enumerate(segments):
            if idx in used_segment_indexes:
                continue
            if is_vague_technical_summary(text) or _summary_related_to_segment(
                text, segment["content"]
            ):
                attach_idx = idx
                break

        if attach_idx is None:
            continue

        segment = segments[attach_idx]
        mem["raw_content"] = segment["content"]
        replacement = text
        if is_vague_technical_summary(text):
            replacement = re.sub(
                r"user (?:sent|shared|provided|submitted|uploaded|pasted|attached|encountered|had|experienced|reported)\b[^.]*\.?",
                "",
                text,
                flags=re.IGNORECASE,
            ).strip()
        mem["text"] = format_memory_with_raw(
            replacement
            or f"User shared {segment['type'].replace('_', ' ')} for future reference.",
            segment["content"],
        )
        mem["memory_type"] = "technical"
        used_segment_indexes.add(attach_idx)

    corpus = _combined_memory_corpus(memories)
    next_id = len(memories)
    for idx, segment in enumerate(segments):
        if idx in used_segment_indexes:
            continue
        if segment_preserved(
            segment["content"],
            corpus,
            segment_type=segment.get("type"),
        ):
            used_segment_indexes.add(idx)
            continue
        label = {
            "code_fence": "code block",
            "shell_command": "shell command",
            "sql": "SQL statement",
            "error_log": "error log",
            "technical_text": "technical content",
        }.get(segment["type"], "technical content")
        memories.append(
            {
                "id": str(next_id),
                "text": format_memory_with_raw(
                    f"User shared {label} for future reference (verbatim preservation).",
                    segment["content"],
                ),
                "raw_content": segment["content"],
                "attributed_to": "user",
                "memory_type": "technical_artifact",
            }
        )
        used_segment_indexes.add(idx)
        corpus = _combined_memory_corpus(memories)
        next_id += 1

    return memories
