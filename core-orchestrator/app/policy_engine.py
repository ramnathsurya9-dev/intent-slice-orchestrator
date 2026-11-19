"""The 80% capacity safeguard and the intent -> profile rules.

Pure functions only (no I/O), so the safety rule is easy to test exhaustively.

Rule: the sum of commercial slice rates (eMBB + URLLC) must never exceed the commercial
budget (80% of capacity). The remaining 20% is the protected reserve of the public slice.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Profile:
    rate_mbps: int  # guaranteed rate
    latency_ms: int  # artificial extra delay (netem); 0 = none
    label: str
    borrows: bool  # may use other slices' idle bandwidth, up to the commercial budget


# Latency note: cloud_gaming gets no artificial delay. Its low latency comes from the highest
# HTB priority, so it is served first whenever the link is busy.
INTENT_PROFILES: dict[str, Profile] = {
    "idle": Profile(2, 50, "low-power", borrows=False),
    "4k_stream": Profile(50, 20, "eMBB", borrows=True),
    "cloud_gaming": Profile(25, 0, "URLLC", borrows=True),
}

# Which commercial slice an intent targets when the caller does not name one.
# "idle" has no default: it applies to every commercial slice.
INTENT_DEFAULT_SLICE = {"4k_stream": "embb_slice", "cloud_gaming": "urllc_slice"}


@dataclass
class Decision:
    allowed: bool
    allocations: dict[str, int]  # commercial rates after the decision (unchanged if rejected)
    scaled: dict[str, tuple[int, int]] = field(default_factory=dict)  # name -> (old, new)
    reason: str = ""


def evaluate(
    current: Mapping[str, int],
    target: str,
    requested_mbps: int,
    *,
    budget: int,
    priorities: Mapping[str, int],
    mode: str,
    floor: int,
) -> Decision:
    """Decide whether `target` may take `requested_mbps` without breaking the budget.

    mode "reject": refuse if the total would exceed the budget.
    mode "scale_down": first shrink slices of LOWER priority than the target (lowest first,
    never below `floor`). A slice is never shrunk to make room for a lower-priority one.
    If that is still not enough, refuse and change nothing.
    """
    if target not in current:
        raise KeyError(f"{target} is not a commercial slice")

    unchanged = dict(current)
    others = {name: rate for name, rate in current.items() if name != target}

    if requested_mbps > budget:
        return Decision(
            False, unchanged,
            reason=f"{requested_mbps} Mbit/s alone is above the commercial budget of {budget} Mbit/s",
        )

    over = sum(others.values()) + requested_mbps - budget
    new = dict(current)
    new[target] = requested_mbps
    if over <= 0:
        return Decision(True, new, reason=f"within budget ({sum(new.values())}/{budget} Mbit/s)")

    if mode != "scale_down":
        return Decision(
            False, unchanged,
            reason=(
                f"would allocate {sum(others.values()) + requested_mbps} Mbit/s to commercial slices; "
                f"the safeguard allows at most {budget} (the rest is the protected public reserve)"
            ),
        )

    scaled: dict[str, tuple[int, int]] = {}
    for name in sorted(others, key=lambda n: priorities[n]):  # lowest priority first
        if over <= 0:
            break
        if priorities[name] >= priorities[target]:
            continue
        cut = min(max(new[name] - floor, 0), over)
        if cut > 0:
            new[name] -= cut
            scaled[name] = (current[name], new[name])
            over -= cut

    if over > 0:
        return Decision(
            False, unchanged,
            reason=(
                f"cannot fit {requested_mbps} Mbit/s even after scaling down lower-priority slices "
                f"({over} Mbit/s still over the {budget} Mbit/s budget)"
            ),
        )
    moved = ", ".join(f"{n} {a}->{b}" for n, (a, b) in scaled.items())
    return Decision(True, new, scaled, reason=f"admitted by scaling down: {moved}")
