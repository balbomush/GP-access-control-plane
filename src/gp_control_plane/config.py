from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import simpleyaml


@dataclass(frozen=True)
class RepoConfig:
    rules: Path
    strategies: Path


@dataclass(frozen=True)
class LocalConfig:
    overrides: Path
    devices: Path
    selected_strategy: Path


@dataclass(frozen=True)
class OutputConfig:
    rendered_dir: Path
    evidence_dir: Path
    state_dir: Path


@dataclass(frozen=True)
class HealthcheckConfig:
    timeout_seconds: float = 5
    retries: int = 2


@dataclass(frozen=True)
class AppConfig:
    repos: RepoConfig
    local: LocalConfig
    output: OutputConfig
    healthcheck: HealthcheckConfig


def load_config(path: Path) -> AppConfig:
    data = simpleyaml.load_file(path)
    if not isinstance(data, dict):
        raise ValueError("config root must be a mapping")

    repos = data.get("repos") or {}
    local = data.get("local") or {}
    output = data.get("output") or {}
    healthcheck = data.get("healthcheck") or {}

    cwd = Path.cwd()
    return AppConfig(
        repos=RepoConfig(
            rules=_resolve(cwd, repos.get("rules", "../GP-traffic-policy-rules")),
            strategies=_resolve(cwd, repos.get("strategies", "../GP-zapret-strategy-catalog")),
        ),
        local=LocalConfig(
            overrides=_resolve(cwd, local.get("overrides", "./site-local-config/local-overrides.yaml")),
            devices=_resolve(cwd, local.get("devices", "./site-local-config/devices.yaml")),
            selected_strategy=_resolve(cwd, local.get("selected_strategy", "./site-local-config/selected-strategy.yaml")),
        ),
        output=OutputConfig(
            rendered_dir=_resolve(cwd, output.get("rendered_dir", "./build/rendered")),
            evidence_dir=_resolve(cwd, output.get("evidence_dir", "./build/evidence")),
            state_dir=_resolve(cwd, output.get("state_dir", "./build/state")),
        ),
        healthcheck=HealthcheckConfig(
            timeout_seconds=float(healthcheck.get("timeout_seconds", 5)),
            retries=int(healthcheck.get("retries", 2)),
        ),
    )


def _resolve(base: Path, value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (base / path).resolve()
