from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class OutputConfig:
    state_dir: Path


@dataclass(frozen=True)
class AppConfig:
    output: OutputConfig


def build_config(state_dir: str | Path | None = None) -> AppConfig:
    value = state_dir or os.environ.get("GP_STATE_DIR") or "./build/state"
    cwd = Path.cwd()
    return AppConfig(
        output=OutputConfig(
            state_dir=_resolve(cwd, value),
        ),
    )


def _resolve(base: Path, value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (base / path).resolve()
