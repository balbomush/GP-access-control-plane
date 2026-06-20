from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


VALID_ROUTES = {"direct", "zapret", "vpn"}


@dataclass(frozen=True)
class Rule:
    id: str
    match: dict[str, Any]
    route: str
    priority: int = 0
    protocols: tuple[str, ...] = ()
    source: str = ""
    comment: str = ""
    origin: str = ""

    @classmethod
    def from_mapping(cls, data: dict[str, Any], origin: str) -> "Rule":
        if not isinstance(data.get("id"), str) or not data["id"]:
            raise ValueError(f"{origin}: rule id is required")
        if not isinstance(data.get("match"), dict) or not data["match"]:
            raise ValueError(f"{origin}: rule {data.get('id')}: match mapping is required")
        if data.get("route") not in VALID_ROUTES:
            raise ValueError(f"{origin}: rule {data.get('id')}: route must be one of {sorted(VALID_ROUTES)}")
        protocols = data.get("protocols") or []
        if not isinstance(protocols, list):
            raise ValueError(f"{origin}: rule {data.get('id')}: protocols must be a list")
        return cls(
            id=data["id"],
            match=data["match"],
            route=data["route"],
            priority=int(data.get("priority") or 0),
            protocols=tuple(str(p) for p in protocols),
            source=str(data.get("source") or ""),
            comment=str(data.get("comment") or ""),
            origin=origin,
        )

    def to_mapping(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "match": self.match,
            "route": self.route,
            "priority": self.priority,
        }
        if self.protocols:
            data["protocols"] = list(self.protocols)
        if self.source:
            data["source"] = self.source
        if self.comment:
            data["comment"] = self.comment
        return data

    def match_key(self) -> str:
        return json.dumps(self.match, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class StrategySelection:
    path: Path
    metadata: dict[str, Any]
    nfqws2_config: Path
