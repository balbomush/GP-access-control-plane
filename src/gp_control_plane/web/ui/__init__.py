"""GP web UI page, split from the former single-file `ui.py`.

The page is assembled server-side from small static parts under
`web/ui/parts/` (see `_assemble.index_html`). Nothing here is ever served
as a separate static file.
"""
from __future__ import annotations

from ._assemble import index_html

__all__ = ["index_html"]
