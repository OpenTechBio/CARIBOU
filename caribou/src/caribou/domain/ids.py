"""Stable, path-safe identifiers for CARIBOU domain records."""

from typing import Final
from uuid import uuid4


ID_PREFIXES: Final[frozenset[str]] = frozenset(
    {
        "spec",
        "exp",
        "run",
        "evt",
        "art",
        "fail",
        "metric",
        "chk",
        "budget",
        "agg",
    }
)


def new_id(kind: str) -> str:
    """Create a collision-resistant identifier with an object-type prefix."""
    if kind not in ID_PREFIXES:
        raise ValueError(f"Unknown CARIBOU identifier kind: {kind!r}")
    return f"{kind}_{uuid4().hex}"
