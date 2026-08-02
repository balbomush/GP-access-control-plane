from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class OutputConfig:
    state_dir: Path


@dataclass(frozen=True)
class InstallConfig:
    root_dir: Path


@dataclass(frozen=True)
class AppConfig:
    output: OutputConfig
    install: InstallConfig = field(default_factory=lambda: InstallConfig(root_dir=default_install_dir()))


def build_config(state_dir: str | Path | None = None, install_dir: str | Path | None = None) -> AppConfig:
    default_root = default_install_dir()
    install_root = _resolve(default_root, install_dir or os.environ.get("GP_INSTALL_DIR") or default_root)
    state_value = state_dir or os.environ.get("GP_STATE_DIR")
    resolved_state_dir = _resolve(install_root, state_value) if state_value else (install_root / "build" / "state").resolve()
    return AppConfig(
        output=OutputConfig(state_dir=resolved_state_dir),
        install=InstallConfig(root_dir=install_root),
    )


def default_install_dir() -> Path:
    current = Path(__file__).resolve()
    for parent in current.parents:
        scripts_dir = parent / "scripts"
        has_installer = (scripts_dir / "install-linux.sh").is_file() or (scripts_dir / "install-raspberry-pi.sh").is_file()
        if (parent / "pyproject.toml").is_file() and has_installer:
            return parent
    return current.parents[2]


def _resolve(base: Path, value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path.resolve()
    return (base / path).resolve()
