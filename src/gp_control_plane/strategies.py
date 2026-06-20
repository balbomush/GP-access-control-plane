from __future__ import annotations

from pathlib import Path

from . import simpleyaml
from .models import StrategySelection


def load_selected_strategy(selection_file: Path) -> StrategySelection | None:
    if not selection_file.exists():
        return None
    data = simpleyaml.load_file(selection_file)
    if not isinstance(data, dict):
        raise ValueError(f"{selection_file}: root must be a mapping")
    strategy_path = data.get("strategy_path")
    if not strategy_path:
        raise ValueError(f"{selection_file}: strategy_path is required")
    base = selection_file.parent
    path = Path(strategy_path)
    if not path.is_absolute():
        path = (base / path).resolve()
    return load_strategy_dir(path)


def load_strategy_dir(path: Path) -> StrategySelection:
    metadata_path = path / "metadata.yaml"
    if not metadata_path.exists():
        raise ValueError(f"{path}: metadata.yaml is required")
    metadata = simpleyaml.load_file(metadata_path)
    if not isinstance(metadata, dict):
        raise ValueError(f"{metadata_path}: root must be a mapping")
    if metadata.get("version") != 1:
        raise ValueError(f"{metadata_path}: version must be 1")
    if not metadata.get("id"):
        raise ValueError(f"{metadata_path}: id is required")
    files = metadata.get("files")
    if not isinstance(files, dict) or not files.get("nfqws2_config"):
        raise ValueError(f"{metadata_path}: files.nfqws2_config is required")
    nfqws2_config = path / str(files["nfqws2_config"])
    if not nfqws2_config.exists():
        raise ValueError(f"{metadata_path}: nfqws2 config does not exist: {nfqws2_config}")
    return StrategySelection(path=path, metadata=metadata, nfqws2_config=nfqws2_config)


def list_local_strategies(strategies_repo: Path) -> list[Path]:
    result: list[Path] = []
    for parent in (strategies_repo / "stable", strategies_repo / "candidates", strategies_repo / "examples"):
        if not parent.exists():
            continue
        for child in sorted(parent.iterdir()):
            if child.is_dir() and (child / "metadata.yaml").exists():
                result.append(child)
    return result
