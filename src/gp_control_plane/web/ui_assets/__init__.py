"""Immutable package resources for the compatibility inline UI document."""

from __future__ import annotations

from functools import lru_cache
from importlib.resources import files
from string import Template


_PACKAGE = "gp_control_plane.web.ui_assets"


class UiAssetError(RuntimeError):
    """A required UI resource is absent from the installed product package."""


def _read_resource(relative_path: str) -> str:
    try:
        return files(_PACKAGE).joinpath(*relative_path.split("/")).read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError) as error:
        raise UiAssetError(f"required UI resource is missing: {relative_path}") from error


def _substitute(template_path: str, **parts: str) -> str:
    try:
        return Template(_read_resource(template_path)).substitute(parts)
    except KeyError as error:
        raise UiAssetError(f"required UI template placeholder is missing: {error.args[0]}") from error


@lru_cache(maxsize=1)
def render_index_html() -> str:
    """Assemble the one legacy inline document from installed UTF-8 resources."""
    shell = _substitute(
        "templates/shell.html",
        finder=_read_resource("templates/tabs/finder.html"),
        history=_read_resource("templates/tabs/history.html"),
        candidates=_read_resource("templates/tabs/candidates.html"),
        terminal=_read_resource("templates/tabs/terminal.html"),
        lists=_read_resource("templates/tabs/lists.html"),
        settings=_read_resource("templates/tabs/settings.html"),
    )
    return _substitute(
        "templates/page.html",
        css=_read_resource("styles/app.css"),
        login=_read_resource("templates/login.html"),
        bootstrap=_read_resource("templates/bootstrap.html"),
        shell=shell,
        script=_read_resource("scripts/legacy-runtime.js"),
    )
