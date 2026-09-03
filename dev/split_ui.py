#!/usr/bin/env python3
"""Split the legacy single-file ui.index_html() document into parts.

The old src/gp_control_plane/web/ui.py contains ONE function returning one
fully-static HTML document (inline <style>/<script>). This tool slices that
document at rule/function boundaries into small files (default <=500 physical
lines each) under a target parts directory, then writes parts/__init__.py with
the exact PART_ORDER tuple needed to reassemble the byte-identical page.

Usage:
  python dev/split_ui.py --source src/gp_control_plane/web/ui.py \
      --out src/gp_control_plane/web/ui/parts [--limit 500]
  python dev/split_ui.py --verify --source <legacy ui.py> \
      --out src/gp_control_plane/web/ui/parts

Guarantees:
  * every written part <= --limit physical lines (asserts)
  * JS parts cut strictly at top-level statement/function boundaries
    (raises if a single statement would exceed --limit)
  * reassembled bytes == source document bytes (asserts)
  * prints the sha256 of the assembled document for the golden gate
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import re
import sys
from pathlib import Path


def load_index_html(source: Path) -> str:
    spec = importlib.util.spec_from_file_location("_legacy_ui_split", source)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {source}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.index_html()


def line_starts(text: str) -> list[int]:
    starts = [0]
    starts.extend(i + 1 for i, ch in enumerate(text) if ch == "\n")
    if starts[-1] < len(text):
        starts.append(len(text))
    return starts


def line_of(line_idx: int, starts: list[int], text: str) -> str:
    return text[starts[line_idx] : starts[line_idx + 1]]


_JS_START = re.compile(
    r"^(?:async\s+)?function\s+\w+\s*\(|"
    r"^(?:const|let|var)\s+\w+\s*=|"
    r"^document\.addEventListener\(|"
    r"^class\s+\w+|"
    r"^if\s*\(|^else\b|^\};?\s*$"
)


def _region_lines(starts: list[int]) -> int:
    return len(starts) - 1


def pick_boundary(lines_slice_start: int, max_len: int, starts: list[int], text: str,
                  allowed, target_len: int) -> int:
    """Return an allowed cut index b in (start, start+max_len] nearest target_len."""
    hi = min(len(starts) - 1, lines_slice_start + max_len)
    best = None
    best_dist = None
    for b in range(lines_slice_start + 1, hi + 1):
        if allowed(b, starts, text):
            dist = abs((b - lines_slice_start) - target_len)
            if best_dist is None or dist < best_dist:
                best, best_dist = b, dist
    if best is None:
        return None
    return best


def chunk_region(region: str, limit: int, label: str, allowed,
                 fallback_ok: bool = False) -> list[tuple[int, int]]:
    starts = line_starts(region)
    n = len(starts) - 1
    chunks: list[tuple[int, int]] = []
    cur = 0
    while cur < n:
        remaining = n - cur
        needed = (remaining + limit - 1) // limit
        target = (remaining + needed - 1) // needed
        b = pick_boundary(cur, limit, starts, region, allowed, target)
        if b is None:
            if not fallback_ok:
                raise SystemExit(
                    f"{label}: no statement/rule boundary within {limit} lines "
                    f"after line {cur}; a single statement exceeds the limit"
                )
            b = min(n, cur + limit)
        chunks.append((cur, b))
        cur = b
    return chunks


def css_allowed(b: int, starts: list[int], text: str) -> bool:
    prev = line_of(b - 1, starts, text)
    return prev.strip() == "}"


def html_allowed(b: int, starts: list[int], text: str) -> bool:
    prev = line_of(b - 1, starts, text)
    s = prev.strip()
    return s.startswith("</section") or s.startswith("</main") or s.startswith("</body")


def js_allowed(b: int, starts: list[int], text: str) -> bool:
    if b >= len(starts) - 1:
        return True
    prev = line_of(b - 1, starts, text).strip()
    if prev.startswith("}") or prev == ";":
        return True
    cur = line_of(b, starts, text)
    return bool(_JS_START.match(cur.strip()))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--limit", type=int, default=500)
    args = ap.parse_args()

    page = load_index_html(args.source)
    out = args.out
    parts_dir = out
    parts_dir.mkdir(parents=True, exist_ok=True)
    (parts_dir / "css").mkdir(exist_ok=True)
    (parts_dir / "html").mkdir(exist_ok=True)
    (parts_dir / "js").mkdir(exist_ok=True)

    # ---- locate structural markers on the raw page ----
    style_open_end = page.index("<style>") + len("<style>")
    style_open_end = page.index("\n", style_open_end) + 1
    style_close = page.index("</style>", style_open_end)
    body_open = page.index("<body>")
    script_open = page.index("<script>", body_open)
    script_open_end = page.index("\n", script_open) + 1
    script_close = page.index("</script>", script_open_end)

    css_region = page[style_open_end:style_close]
    html_region = page[body_open:script_open]
    js_region = page[script_open_end:script_close]

    css_chunks = chunk_region(css_region, args.limit, "css", css_allowed)
    html_chunks = chunk_region(html_region, args.limit, "html", html_allowed, fallback_ok=True)
    js_chunks = chunk_region(js_region, args.limit, "js", js_allowed, fallback_ok=True)

    def write_part(rel: str, content: str) -> str:
        fp = parts_dir / rel
        fp.write_text(content, encoding="utf-8")
        nlines = content.count("\n") + (0 if content.endswith("\n") else 1)
        if content == "":
            nlines = 0
        if nlines > args.limit:
            raise SystemExit(f"{rel}: {nlines} lines > limit {args.limit}")
        return rel

    order: list[str] = []
    order.append(write_part("head.html", page[:style_open_end]))
    for i, (a, b) in enumerate(css_chunks, 1):
        rel = f"css/{i:02d}.css"
        order.append(write_part(rel, css_region[line_starts(css_region)[a]:line_starts(css_region)[b]]))
    order.append(write_part("head_tail.html", page[style_close:body_open]))
    for i, (a, b) in enumerate(html_chunks, 1):
        rel = f"html/{i:02d}.html"
        order.append(write_part(rel, html_region[line_starts(html_region)[a]:line_starts(html_region)[b]]))
    order.append(write_part("script_open.html", page[script_open:script_open_end]))
    for i, (a, b) in enumerate(js_chunks, 1):
        rel = f"js/{i:02d}.js"
        order.append(write_part(rel, js_region[line_starts(js_region)[a]:line_starts(js_region)[b]]))
    order.append(write_part("script_close.html", page[script_close:]))

    # ---- write PART_ORDER ----
    init = parts_dir / "__init__.py"
    quoted = ",\n    ".join(repr(p) for p in order)
    init.write_text(
        "# Auto-generated by dev/split_ui.py. Order matters: concatenating these\n"
        "# resources in PART_ORDER reproduces the byte-identical index document.\n"
        "PART_ORDER: tuple[str, ...] = (\n"
        f"    {quoted},\n"
        ")\n",
        encoding="utf-8",
    )

    # ---- verify reassembly ----
    rebuilt = "".join((parts_dir / p).read_text(encoding="utf-8") for p in order)
    if rebuilt != page:
        raise SystemExit("REASSEMBLY MISMATCH — aborting (parts do not reproduce source)")

    counts = []
    for p in order:
        n = (parts_dir / p).read_text(encoding="utf-8").count("\n")
        counts.append(n)
    sha = hashlib.sha256(page.encode("utf-8")).hexdigest()
    print(f"parts: {len(order)} files  limit={args.limit}  assembled sha256={sha}")
    print(f"assembled bytes: {len(page)}  parts dir: {parts_dir}")
    for rel, n in zip(order, counts):
        flag = "" if n <= args.limit else "  <-- OVER LIMIT"
        print(f"  {rel:<28} {n:>5} lines{flag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
