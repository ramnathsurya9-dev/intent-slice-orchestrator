"""A small model of HTB sharing: guarantees first, then idle bandwidth by priority.

Used to produce simulated telemetry in dry-run mode, and to unit-test the sharing rules that the
kernel's HTB tree is configured to follow. It is a model, not the kernel: real HTB splits equal
priorities by rate and has token-bucket dynamics.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Flow:
    name: str
    rate: float  # guaranteed
    ceil: float  # maximum
    prio: int  # lower number = served first when borrowing
    demand: float  # what the tenant is trying to send
    commercial: bool  # shares the commercial class (hard cap)


def allocate(link: float, commercial_cap: float, flows: Sequence[Flow]) -> dict[str, float]:
    """Return the throughput each flow gets."""
    granted = {f.name: min(f.demand, f.rate) for f in flows}  # 1. everyone's guarantee

    def commercial_used() -> float:
        return sum(granted[f.name] for f in flows if f.commercial)

    for f in sorted(flows, key=lambda x: x.prio):  # 2. idle bandwidth, most important first
        room = min(f.demand, f.ceil) - granted[f.name]
        room = min(room, link - sum(granted.values()))
        if f.commercial:
            room = min(room, commercial_cap - commercial_used())
        granted[f.name] += max(room, 0.0)
    return granted
