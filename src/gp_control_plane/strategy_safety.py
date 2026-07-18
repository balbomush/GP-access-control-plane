from __future__ import annotations

import re
import shlex
from dataclasses import dataclass


FRAGMENTATION_POSITION_FREE = "position_free"
FRAGMENTATION_POSITION_SAFE = "position_safe"
FRAGMENTATION_POSITION_RISKY = "position_risky"
FRAGMENTATION_UNKNOWN = "unknown"
FRAGMENTATION_CLASSES = {
    FRAGMENTATION_POSITION_FREE,
    FRAGMENTATION_POSITION_SAFE,
    FRAGMENTATION_POSITION_RISKY,
    FRAGMENTATION_UNKNOWN,
}
FRAGMENTATION_SAFE_CLASSES = {FRAGMENTATION_POSITION_FREE, FRAGMENTATION_POSITION_SAFE}

FAMILY_ORDER = {
    "hostfakesplit": 10,
    "fakedsplit": 20,
    "multidisorder": 30,
    "wssize": 40,
    "fake": 50,
    "split": 60,
    "disorder": 70,
    "tamper": 80,
    "ttl/autottl": 90,
    "ipfrag": 100,
    "udp/quic": 110,
    "tlsrec": 120,
    "other": 900,
}

_POSITION_MARKERS = (
    "pos=",
    "sniext",
    "host+",
    "host-",
    "midsld",
    "endhost",
    "marker+",
    "marker-",
    "split-pos",
)
_FREE_MODES = {"fake", "common"}
_SAFE_RELATIVE_MARKERS = ("sniext", "host+", "host-", "midsld", "endhost")


@dataclass(frozen=True)
class StrategyAnalysis:
    fragmentation_class: str
    fragmentation_safe: bool
    fragmentation_reason: str
    family: str
    family_key: str
    family_rank: int
    family_reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "fragmentation_class": self.fragmentation_class,
            "fragmentation_safe": self.fragmentation_safe,
            "fragmentation_reason": self.fragmentation_reason,
            "family": self.family,
            "family_key": self.family_key,
            "family_rank": self.family_rank,
            "family_reason": self.family_reason,
        }


def analyze_strategy(protocol: str, args: str) -> StrategyAnalysis:
    text = _normalize_args(args)
    family, family_reason = _classify_family(protocol, text)
    fragmentation_class, fragmentation_reason = _classify_fragmentation(text, family)
    return StrategyAnalysis(
        fragmentation_class=fragmentation_class,
        fragmentation_safe=fragmentation_class in FRAGMENTATION_SAFE_CLASSES,
        fragmentation_reason=fragmentation_reason,
        family=family,
        family_key=_family_key(family, protocol, text),
        family_rank=FAMILY_ORDER.get(family, FAMILY_ORDER["other"]),
        family_reason=family_reason,
    )


def normalize_strategy_args(args: str) -> str:
    return _normalize_args(args)


def _normalize_args(args: str) -> str:
    try:
        parts = shlex.split(str(args or ""))
    except ValueError:
        parts = str(args or "").split()
    return " ".join(parts).strip()


def _classify_family(protocol: str, text: str) -> tuple[str, str]:
    lower = text.lower()
    proto = str(protocol or "").lower()
    if proto == "quic" or "--filter-udp" in lower or "http3" in lower:
        return "udp/quic", "QUIC/UDP strategy"
    for family, markers in (
        ("hostfakesplit", ("hostfakesplit",)),
        ("fakedsplit", ("fakedsplit", "fakeddisorder")),
        ("multidisorder", ("multidisorder", "multisplit")),
        ("wssize", ("wssize", "wsize=")),
        ("tlsrec", ("tlsrec", "tlsrec=")),
        ("ipfrag", ("ipfrag", "ipfrag=", "ipfrag2")),
        ("ttl/autottl", ("autottl", "ttl=", "--dpi-desync-ttl", "--dpi-desync-autottl")),
        ("tamper", ("hostcase", "hostnospace", "domcase", "methodeol", "unixeol", "tamper")),
        ("fake", ("desync=fake", "desync=common")),
        ("split", ("desync=split", "split-pos", "split-seqovl")),
        ("disorder", ("desync=disorder", "disorder")),
    ):
        if any(marker in lower for marker in markers):
            return family, f"matched {family} marker"
    return "other", "no known family marker"


def _classify_fragmentation(text: str, family: str) -> tuple[str, str]:
    lower = text.lower()
    modes = _desync_modes(lower)
    if family == "udp/quic":
        return FRAGMENTATION_POSITION_FREE, "QUIC/UDP strategy is not tied to TLS ClientHello byte positions"
    if not modes:
        if any(marker in lower for marker in _POSITION_MARKERS):
            return FRAGMENTATION_UNKNOWN, "position markers found without a recognized desync mode"
        return FRAGMENTATION_UNKNOWN, "no recognized desync mode"
    if modes <= _FREE_MODES and not any(marker in lower for marker in _POSITION_MARKERS):
        return FRAGMENTATION_POSITION_FREE, "fake/common mode has no explicit split position"
    if _has_numeric_position(lower):
        return FRAGMENTATION_POSITION_RISKY, "strategy uses numeric or fixed split position"
    if any(marker in lower for marker in _SAFE_RELATIVE_MARKERS):
        return FRAGMENTATION_POSITION_SAFE, "strategy uses named TLS/host-relative split markers"
    if any(mode in modes for mode in ("split", "multisplit", "fakedsplit", "hostfakesplit", "multidisorder")):
        return FRAGMENTATION_UNKNOWN, "split-like strategy without a clear position marker"
    if "disorder" in modes or family in {"disorder", "multidisorder"}:
        return FRAGMENTATION_POSITION_SAFE, "disorder strategy has no fixed numeric split position"
    return FRAGMENTATION_UNKNOWN, "recognized strategy family but fragmentation position is unclear"


def _desync_modes(lower: str) -> set[str]:
    modes: set[str] = set()
    for match in re.finditer(r"(?:lua|dpi)-desync=([^ \t]+)", lower):
        raw = match.group(1)
        mode_part = raw.split(":", 1)[0]
        for item in mode_part.split(","):
            clean = item.strip()
            if clean:
                modes.add(clean)
    return modes


def _has_numeric_position(lower: str) -> bool:
    if re.search(r"(?:^|[: ,])pos=-?\d+(?:[,: ]|$)", lower):
        return True
    if re.search(r"split-pos=-?\d+", lower):
        return True
    return False


def _family_key(family: str, protocol: str, text: str) -> str:
    proto = str(protocol or "").strip().lower() or "unknown"
    tokens = []
    for raw in text.split():
        item = raw.strip()
        lower = item.lower()
        if lower.startswith("--payload"):
            tokens.append(_option_name(lower))
        elif "desync=" in lower:
            tokens.append(_desync_family_token(lower))
        elif any(marker in lower for marker in ("wsize=", "ttl", "ipfrag", "tlsrec", "hostcase", "hostnospace")):
            tokens.append(_option_name(lower))
    key_tail = "+".join(token for token in tokens if token)
    return f"{proto}:{family}:{key_tail or 'default'}"


def _option_name(token: str) -> str:
    return token.split("=", 1)[0].split(":", 1)[0]


def _desync_family_token(token: str) -> str:
    match = re.search(r"(?:lua|dpi)-desync=([^ \t:]+)", token)
    if not match:
        return _option_name(token)
    return f"desync={match.group(1).split(',', 1)[0]}"
