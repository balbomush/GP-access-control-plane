from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import simpleyaml


@dataclass(frozen=True)
class OutputConfig:
    state_dir: Path


@dataclass(frozen=True)
class AppConfig:
    output: OutputConfig


def load_config(path: Path) -> AppConfig:
    data = simpleyaml.load_file(path)
    if not isinstance(data, dict):
        raise ValueError("config root must be a mapping")

    output = data.get("output") or {}

    cwd = Path.cwd()
    return AppConfig(
        output=OutputConfig(
            state_dir=_resolve(cwd, output.get("state_dir", "./build/state")),
        ),
    )


def _resolve(base: Path, value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (base / path).resolve()
