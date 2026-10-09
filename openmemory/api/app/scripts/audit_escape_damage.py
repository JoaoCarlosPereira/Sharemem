"""Auditoria de dano de escape no acervo (problema 4a).

    python -m app.scripts.audit_escape_damage [--base http://192.168.3.213:8765]

Lista as memorias cujo texto carrega caracteres de controle C0 - a assinatura de
uma sequencia de escape desfeita indevidamente durante a extracao pelo LLM (ver
``mem0.memory.technical_content.repair_escape_damage``). Nao altera nada: e um
relatorio. Rodada em 01/09/2026 sobre 12.661 memorias, achou 11.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request

CONTROL_CHARS = re.compile(r"[\x00-\x09\x0b\x0c\x0e-\x1f]")


def _get(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path, timeout=60) as resp:
        return json.load(resp)


def iter_memories(base: str):
    """Percorre todos os projetos e paginas do acervo."""
    for app in _get(base, "/api/v1/apps/?page_size=200")["apps"]:
        if not app["total_memories_created"]:
            continue
        page = 1
        while True:
            query = urllib.parse.urlencode({"page_size": 100, "page": page})
            data = _get(base, "/api/v1/apps/%s/memories?%s" % (app["id"], query))
            items = data.get("memories", [])
            if not items:
                break
            for item in items:
                yield app["name"], item
            if len(items) < 100 or page * 100 >= (data.get("total") or 0):
                break
            page += 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://192.168.3.213:8765")
    args = parser.parse_args()

    total = 0
    damaged = []
    for project, item in iter_memories(args.base):
        total += 1
        text = item.get("content") or item.get("text") or ""
        if CONTROL_CHARS.search(text):
            damaged.append((project, item, text))

    print("varridas: %d memorias" % total)
    print("com dano de escape: %d" % len(damaged))
    for project, item, text in damaged:
        controls = sorted({hex(ord(c)) for c in text if CONTROL_CHARS.match(c)})
        snippet = " ".join(CONTROL_CHARS.sub("<CTRL>", text).split())[:160]
        print()
        print("  %s  [%s]  %s" % (item["id"], project, item.get("created_at")))
        print("    controles: %s" % ", ".join(controls))
        print("    %s" % snippet)
    return 1 if damaged else 0


if __name__ == "__main__":
    sys.exit(main())
