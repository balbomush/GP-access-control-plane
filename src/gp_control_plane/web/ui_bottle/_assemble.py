"""Server-side assembly for Bottle responsive Web UI."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from gp_control_plane.web.ui import index_html as base_index_html


@lru_cache(maxsize=1)
def bottle_index_html() -> str:
    """Return index HTML injected with Bottle responsive mobile/desktop CSS."""
    base = base_index_html()
    css_path = Path(__file__).parent / "css" / "01_responsive.css"
    if css_path.is_file():
        css = css_path.read_text(encoding="utf-8")
        style_tag = f"<style>\n{css}\n</style>\n</head>"
        if "</head>" in base:
            return base.replace("</head>", style_tag, 1)
    return base
