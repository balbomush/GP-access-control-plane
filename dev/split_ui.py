#!/usr/bin/env python3
"""Split/regenerate the GP web UI page into semantically-named parts.

The page (formerly the single `web/ui.py::index_html()`) is a fully static
HTML document with one inline <style> and one inline <script>. This tool
re-slices it into small files (each <=500 physical lines) stored under
`web/ui/parts/`, then rewrites `parts/__init__.py::PART_ORDER` so that
concatenating the parts reproduces the byte-identical document.

Slicing rules (clean, nameable boundaries):
  * CSS  : cut only between complete rules (after a `}` line)
  * HTML : cut at top-level <section> boundaries -> one file per UI feature
           (login / shell-header+metrics / each tab-panel / toast+closes)
  * JS   : a JS-aware scanner (strings/templates/comments/regex + {} depth)
           finds valid cut points at top-level statement ends; explicit seams
           (top-level function names) delimit feature parts with meaningful
           names; the last seam lands right after the final top-level function
           (`stopCurrentJob`), so the trailing event-wiring/boot chunk is whole.

Guarantees (asserted, exits non-zero on violation):
  * reassembly of the written parts == source page
  * sha256 of assembled page matches the pre-split golden value
  * every part <= --limit physical lines
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

GOLDEN_SHA256 = "60bcbdabd902e4765a64358bef00815486bb3415518543bf1ae17ec04d806249"

# CSS part labels in assembly order (boundaries kept at rule ends).
CSS_LABELS = [
    "tokens_login_metrics",      # :root tokens, login, topbar, status/metrics
    "launch_presets_settings",   # run-launch-summary, presets, settings/releases
    "candidates_backups_live",   # candidate-result, backups, editors, live-run
    "progress_events_history",   # progress, events, messages/toast, tables, run-history, @media
]

# HTML tab-panel ids, in document order.
HTML_TABS = ["finder", "history", "candidates", "terminal", "lists", "settings"]

# JS parts: (label, boundary_anchor). Each label is a part; boundary_anchor is
# the top-level function that starts the FOLLOWING part (a cut happens right
# before it). None means the part extends to the next special boundary
# (realtime -> boot tail; boot -> end of the script region).
JS_PARTS = [
    ("globals_helpers_fetch", "authToken"),
    ("auth_session", "apiUrl"),
    ("utils_tabs_run_form", "hideFieldRow"),
    ("engine_launch_preferences", "loadCustomPresets"),
    ("presets_core_manager", "statusCheck"),
    ("status_metrics_candidates", "buildCandidateResult"),
    ("candidate_result_build", "filterTestedDomains"),
    ("common_domains_picker", "candidateGroups"),
    ("strategies_family_editors", "renderRuns"),
    ("runs_history_replay", "runProgressText"),
    ("log_progress_live_events", "renderRunSettingsSummary"),
    ("settings_releases", "v2flyCategoryName"),
    ("v2fly_preset_manager_editor", "formatDuration"),
    ("render_refresh_candidates", "handleCandidateEvent"),
    ("realtime_refresh_backups_jobs", None),  # runs to the boot-tail seam
    ("boot_event_wiring", None),              # runs to the end of the script
]

LAST_JS_FUNCTION = "stopCurrentJob"


def load_page() -> str:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from gp_control_plane.web.ui import index_html

    return index_html()


class LineView:
    """Uniform line view over a text region."""

    __slots__ = ("text", "starts", "n")

    def __init__(self, text: str) -> None:
        self.text = text
        starts = [0]
        starts.extend(i + 1 for i, ch in enumerate(text) if ch == "\n")
        if starts[-1] < len(text):
            starts.append(len(text))
        self.starts = starts
        self.n = len(starts) - 1

    def line(self, k: int) -> str:
        return self.text[self.starts[k] : self.starts[k + 1]]

    def offset(self, k: int) -> int:
        return self.starts[k]

    def slice(self, a: int, b: int) -> str:
        # lines [a, b)
        return self.text[self.starts[a] : self.starts[b]]


# ---------------- CSS ----------------
def css_chunks(view: LineView, limit: int) -> list[tuple[int, int]]:
    n = view.n
    allowed = [b for b in range(1, n + 1) if view.line(b - 1).strip() == "}"]
    allowed.append(n)
    chunks_needed = (n + limit - 1) // limit
    target = (n + chunks_needed - 1) // chunks_needed
    out: list[tuple[int, int]] = []
    cur = 0
    while cur < n:
        hi = min(n, cur + limit)
        cand = [b for b in allowed if cur < b <= hi]
        best = min(cand, key=lambda b: abs((b - cur) - target)) if cand else hi
        out.append((cur, best))
        cur = best
    return out


# ---------------- HTML ----------------
def html_parts(view: LineView) -> list[tuple[int, int, str]]:
    """Return [(start_line, end_line_exclusive, label), ...] covering [0, n)."""
    n = view.n
    depth = 0
    open_line: int | None = None
    open_id = ""
    spans: list[tuple[int, int, str]] = []
    for i in range(n):
        raw = view.line(i)
        opens = raw.count("<section")
        closes = raw.count("</section")
        if depth == 0 and opens:
            open_line = i
            m = raw.find('id="')
            open_id = raw[m + 4 :].split('"', 1)[0] if m != -1 else ""
        depth += opens - closes
        if depth == 0 and open_line is not None:
            spans.append((open_line, i, open_id))
            open_line = None
            open_id = ""

    login = next((s for s in spans if "login" in s[2]), None)
    tab_spans = {s[2]: s for s in spans if s[2].startswith("tab-panel-")}
    tab_list = [tab_spans[f"tab-panel-{t}"] for t in HTML_TABS if f"tab-panel-{t}" in tab_spans]

    # block boundaries (start line of each block); blocks tile [0, n) exactly
    blocks: list[tuple[str, int, int]] = []  # (label, start, end_exclusive)
    if login is not None:
        b0 = 0
        b1 = login[1] + 1
        blocks.append(("login", b0, b1))
        if tab_list and b1 < tab_list[0][0]:
            blocks.append(("shell_header_metrics", b1, tab_list[0][0]))
    for idx, span in enumerate(tab_list):
        s = span[0]
        if idx + 1 < len(tab_list):
            e = tab_list[idx + 1][0]
        else:
            e = span[1] + 1
        label = span[2].replace("tab-panel-", "")
        blocks.append((label, s, e))
    if tab_list:
        tail_start = tab_list[-1][1] + 1
        if tail_start < n:
            blocks.append(("toast_close", tail_start, n))
    return [(s, e, label) for label, s, e in blocks if e > s]


# ---------------- JS ----------------
def js_valid_cuts(view: LineView) -> set[int]:
    """Boundary line indices k (1..view.n) where a top-level cut is valid."""
    text = view.text
    n = len(text)
    off_to_k = {off: k for k, off in enumerate(view.starts)}
    i = 0
    depth = 0
    last_sig = ""
    in_block = False
    in_template = False
    allowed: set[int] = set()
    while i < n:
        c = text[i]
        if in_block:
            if c == "*" and i + 1 < n and text[i + 1] == "/":
                in_block = False
                i += 2
            else:
                i += 1
            continue
        if in_template:
            if c == "\\":
                i += 2
                continue
            if c == "`":
                in_template = False
            i += 1
            continue
        if c == "/" and i + 1 < n:
            nx = text[i + 1]
            if nx == "/":
                j = text.find("\n", i)
                i = n if j == -1 else j + 1
                continue
            if nx == "*":
                in_block = True
                i += 2
                continue
            looks_regex = not last_sig or last_sig in "([{=,:;!&|?+-*%^~<>"
            if looks_regex:
                j = i + 2
                in_class = False
                while j < n and text[j] not in "\r\n":
                    ch = text[j]
                    if ch == "\\":
                        j += 2
                        continue
                    if ch == "[":
                        in_class = True
                    elif ch == "]":
                        in_class = False
                    elif ch == "/" and not in_class:
                        break
                    j += 1
                if j < n and text[j] == "/":
                    j += 1
                last_sig = "/"
                i = j
                continue
            last_sig = "/"
            i += 1
            continue
        if c in "\"'`":
            if c == "`":
                in_template = True
                i += 1
                continue
            quote = c
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == quote:
                    j += 1
                    break
                j += 1
            last_sig = quote
            i = j
            continue
        if c in "([{":
            depth += 1
            last_sig = c
            i += 1
            continue
        if c in ")]}":
            depth = max(0, depth - 1)
            last_sig = c
            i += 1
            continue
        if c == "\n":
            nxt = i + 1
            k = off_to_k.get(nxt)
            if k is not None and depth == 0 and last_sig in ";}" and not in_template:
                allowed.add(k)
        if not c.isspace():
            last_sig = c
        i += 1
    return allowed


def _top_level_function_lines(view: LineView) -> dict[str, int]:
    out: dict[str, int] = {}
    for i in range(view.n):
        raw = view.line(i)
        stripped = raw.strip()
        if stripped.startswith("function "):
            rest = stripped[len("function ") :]
            name = rest.split("(", 1)[0].strip()
            if name and raw[:1] not in (" ", "\t"):
                out.setdefault(name, i)
        elif stripped.startswith("async function "):
            rest = stripped[len("async function ") :]
            name = rest.split("(", 1)[0].strip()
            if name and raw[:1] not in (" ", "\t"):
                out.setdefault(name, i)
    return out


def js_parts(view: LineView, limit: int) -> list[tuple[str, int, int]]:
    n = view.n
    allowed = js_valid_cuts(view)
    funcs = _top_level_function_lines(view)

    def anchor_line(name: str, what: str) -> int:
        line = funcs.get(name)
        if line is None:
            raise SystemExit(f"JS anchor '{name}' not found as a top-level function ({what})")
        if line not in allowed:
            cand = sorted(a for a in allowed if a >= line)
            if not cand:
                raise SystemExit(f"no valid cut at/after '{name}' ({what})")
            line = cand[0]
        return line

    boundaries = [0]
    for label, anchor in JS_PARTS:
        if anchor is not None:
            boundaries.append(anchor_line(anchor, label))
    last_func = funcs.get(LAST_JS_FUNCTION)
    if last_func is None:
        raise SystemExit(f"last JS function '{LAST_JS_FUNCTION}' not found")
    tail = sorted(a for a in allowed if a > last_func)
    tail_seam = tail[0] if tail else n
    if tail_seam <= boundaries[-1]:
        raise SystemExit("boot tail seam is not after the last anchored part")
    boundaries.append(tail_seam)
    boundaries.append(n)

    labels = [lab for lab, _ in JS_PARTS]
    if len(boundaries) != len(labels) + 1:
        raise SystemExit(f"boundary/label mismatch: {len(boundaries)} vs {len(labels)}")
    parts: list[tuple[str, int, int]] = []
    for k, label in enumerate(labels):
        a, b = boundaries[k], boundaries[k + 1]
        if b - a > limit:
            raise SystemExit(f"js part '{label}': {b - a} lines > limit {limit}")
        if b <= a:
            raise SystemExit(f"js part '{label}' has empty span")
        parts.append((label, a, b))
    return parts


# ---------------- writer ----------------
def _write(out: Path, name: str, content: str, limit: int) -> None:
    fp = out / name
    fp.write_text(content, encoding="utf-8")
    nl = content.count("\n")
    if nl > limit:
        raise SystemExit(f"{name}: {nl} lines > limit {limit}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--page-file", type=Path, default=None,
                    help="read the source page text from this file instead of the ui package")
    args = ap.parse_args()

    if args.page_file is not None:
        page = args.page_file.read_text(encoding="utf-8")
    else:
        page = load_page()
    repo = Path(__file__).resolve().parents[1]
    out = (args.out or repo / "src/gp_control_plane/web/ui/parts").resolve()
    out.mkdir(parents=True, exist_ok=True)

    style_open_end = page.index("<style>") + len("<style>")
    style_open_end = page.index("\n", style_open_end) + 1
    style_close = page.index("</style>", style_open_end)
    body_open = page.index("<body>")
    script_open = page.index("<script>", body_open)
    script_open_end = page.index("\n", script_open) + 1
    script_close = page.index("</script>", script_open_end)

    css_view = LineView(page[style_open_end:style_close])
    html_view = LineView(page[body_open:script_open])
    js_view = LineView(page[script_open_end:script_close])

    # clear previous generated layout
    for sub in ("css", "html", "js"):
        d = out / sub
        if d.exists():
            for p in d.glob("*"):
                p.unlink()
        else:
            d.mkdir()
    for name in list(out.glob("*.html")) + list(out.glob("*.css")) + list(out.glob("*.js")):
        name.unlink()

    order: list[str] = []

    def add(name: str, content: str) -> None:
        _write(out, name, content, args.limit)
        order.append(name)

    add("head.html", page[:style_open_end])

    for k, ((a, b), label) in enumerate(zip(css_chunks(css_view, args.limit), CSS_LABELS), 1):
        add(f"css/{k:02d}_{label}.css", css_view.slice(a, b))

    add("head_close.html", page[style_close:body_open])

    for num, (a, b, label) in enumerate(html_parts(html_view), 1):
        add(f"html/{num:02d}_{label}.html", html_view.slice(a, b))

    add("script_open.html", page[script_open:script_open_end])

    for num, (label, a, b) in enumerate(js_parts(js_view, args.limit), 1):
        add(f"js/{num:02d}_{label}.js", js_view.slice(a, b))

    add("script_close.html", page[script_close:])

    quoted = ",\n    ".join(repr(p) for p in order)
    (out / "__init__.py").write_text(
        "# Auto-generated by dev/split_ui.py. Concatenating these resources in\n"
        "# PART_ORDER reproduces the byte-identical index document.\n"
        "PART_ORDER: tuple[str, ...] = (\n"
        f"    {quoted},\n"
        ")\n",
        encoding="utf-8",
    )

    rebuilt = "".join((out / p).read_text(encoding="utf-8") for p in order)
    sha = hashlib.sha256(rebuilt.encode("utf-8")).hexdigest()
    if rebuilt != page:
        raise SystemExit("REASSEMBLY MISMATCH")
    if sha != GOLDEN_SHA256:
        raise SystemExit(f"sha mismatch: {sha} != golden {GOLDEN_SHA256}")

    print(f"parts: {len(order)}  limit={args.limit}  assembled sha256={sha}")
    for name in order:
        content = (out / name).read_text(encoding="utf-8")
        print(f"  {name:<48} {content.count(chr(10)):>5} lines")
    return 0


if __name__ == "__main__":
    sys.exit(main())
