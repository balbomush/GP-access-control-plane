from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from . import simpleyaml
from .validation import assert_no_secret_like_mapping


def write_evidence(
    evidence_dir: Path,
    *,
    rule_id: str,
    result: str,
    checks: int,
    success_rate: float,
    network_id: str | None = None,
) -> Path:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "network_id": network_id or f"anonymous-{uuid.uuid4().hex[:12]}",
        "rule_id": rule_id,
        "result": result,
        "checks": checks,
        "success_rate": success_rate,
        "tested_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    assert_no_secret_like_mapping(payload)
    path = evidence_dir / f"{payload['tested_at'].replace(':', '').replace('-', '')}-{rule_id}.yaml"
    simpleyaml.dump_file(path, payload)
    return path
