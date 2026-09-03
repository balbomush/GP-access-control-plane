"""Server-side assembly of the GP web UI document.

Replaces the former single-file `web/ui.py` (one `index_html()` returning a
~6.9k-line inline HTML/CSS/JS document). The document is now stored as small
static part files under `web/ui/parts/` (each <=500 lines) and joined here in
`PART_ORDER` at first call, so the served page stays byte-identical to the
legacy output while nothing new is ever exposed over the network (parts are
never served as static files — they are read server-side and inlined).
"""
from __future__ import annotations

from functools import lru_cache
from importlib.resources import files

from .parts import PART_ORDER


def _join_parts() -> str:
    root = files(__package__ + ".parts")
    chunks: list[str] = []
    for rel in PART_ORDER:
        node = root
        for segment in rel.split("/"):
            node = node.joinpath(segment)
        chunks.append(node.read_text(encoding="utf-8"))
    return "".join(chunks)


@lru_cache(maxsize=1)
def index_html() -> str:
    return _join_parts()
