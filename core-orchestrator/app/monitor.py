"""Telemetry loop: reads HTB class counters every few seconds and reacts to congestion by itself."""

from __future__ import annotations

import asyncio
import logging
import subprocess

from .htb_model import Flow, allocate
from .service import Orchestrator

log = logging.getLogger("monitor")


def evaluate_congestion(
    public_drop_pct: float, total_mbps: float, capacity_mbps: int,
    drop_threshold_pct: float, saturation_pct: float,
) -> str | None:
    """Return a reason if the link counts as congested, else None.

    A busy link is normal now that idle bandwidth is lent out, so saturation only counts when the
    public slice is already losing packets.
    """
    if public_drop_pct > drop_threshold_pct:
        return f"public slice dropping {public_drop_pct:.1f}% of packets (limit {drop_threshold_pct}%)"
    if total_mbps >= capacity_mbps * saturation_pct / 100 and public_drop_pct > 0:
        return f"link saturated ({total_mbps:.0f} Mbit/s) and the public slice is losing packets"
    return None


def _ping_ms(ip: str) -> float | None:
    try:
        out = subprocess.run(
            ["ping", "-c", "1", "-W", "1", ip], check=False, capture_output=True, text=True, timeout=3
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    marker = "time="
    if marker not in out:
        return None
    try:
        return float(out.split(marker)[1].split()[0].rstrip("ms"))
    except (ValueError, IndexError):
        return None


class Monitor:
    def __init__(self, orch: Orchestrator) -> None:
        self.orch = orch
        self.clean = 0
        self._prev: dict[str, dict] = {}

    # ----- sampling -----
    @staticmethod
    def _entry(s, tx_mbps: float, drop_pct: float, drops: int, backlog: int, rtt) -> dict:
        return {
            "tx_mbps": tx_mbps, "drop_pct": drop_pct, "drops": drops, "backlog_bytes": backlog,
            "borrowing_mbps": max(tx_mbps - s.rate_mbps, 0.0), "rtt_ms": rtt,
        }

    def _real(self, now: float) -> dict[str, dict]:
        orch = self.orch
        classes = orch.traffic.class_stats(orch.settings.shaping_iface)
        out: dict[str, dict] = {}
        for s in orch.slices.values():
            c = classes.get(s.class_minor)
            if c is None:
                out[s.name] = {"error": f"no HTB class 1:{s.class_minor} on {orch.settings.shaping_iface}"}
                continue
            prev = self._prev.get(s.name)
            self._prev[s.name] = {"t": now, **c}
            tx = drop_pct = 0.0
            if prev and now > prev["t"]:
                tx = max(c["sent_bytes"] - prev["sent_bytes"], 0) * 8 / (now - prev["t"]) / 1e6
                d_drop = max(c["dropped"] - prev["dropped"], 0)
                d_pkts = max(c["sent_pkts"] - prev["sent_pkts"], 0)
                if d_drop + d_pkts > 0:
                    drop_pct = d_drop / (d_drop + d_pkts) * 100
            out[s.name] = self._entry(s, tx, drop_pct, c["dropped"], c["backlog_bytes"], _ping_ms(s.ip))
        return out

    def _simulated(self, now: float) -> dict[str, dict]:
        """Model what HTB would do: demand -> guarantees first, then idle bandwidth by priority."""
        orch, cfg = self.orch, self.orch.settings
        flooded = now < orch.sim_congestion_until
        flows, demand = [], {}
        for s in orch.slices.values():
            if not s.commercial:
                demand[s.name] = float(cfg.total_capacity_mbps) if flooded else cfg.public_reserve_mbps * 0.4
            elif s.intent == "idle":
                demand[s.name] = s.rate_mbps * 0.5
            else:
                demand[s.name] = float(s.ceiling_mbps)  # an active tenant wants all it may have
            flows.append(
                Flow(s.name, s.rate_mbps, s.ceiling_mbps, 2 - s.priority, demand[s.name], s.commercial)
            )
        share = allocate(cfg.total_capacity_mbps, cfg.commercial_budget_mbps, flows)
        out = {}
        for s in orch.slices.values():
            lost = max(demand[s.name] - share[s.name], 0.0)
            drop_pct = lost / demand[s.name] * 100 if demand[s.name] else 0.0
            out[s.name] = self._entry(s, share[s.name], drop_pct, 0, 0, float(s.latency_ms) + 0.4)
        return out

    def collect(self) -> dict[str, dict]:
        now = self.orch.clock()
        try:
            return self._simulated(now) if self.orch.settings.dry_run else self._real(now)
        except Exception as exc:  # noqa: BLE001 - a bad reading must not stop the loop
            return {s.name: {"error": str(exc)} for s in self.orch.slices.values()}

    # ----- decision -----
    def step(self) -> None:
        orch, cfg = self.orch, self.orch.settings
        orch.telemetry = self.collect()
        public = orch.telemetry.get("public_slice", {})
        total = sum(t.get("tx_mbps", 0.0) for t in orch.telemetry.values())
        reason = evaluate_congestion(
            public.get("drop_pct", 0.0), total, cfg.total_capacity_mbps,
            cfg.drop_threshold_pct, cfg.saturation_pct,
        )
        if reason:
            self.clean = 0
            orch.emergency_throttle(reason)
        elif orch.throttled:
            self.clean += 1
            if self.clean >= cfg.release_after_clean:
                orch.release_emergency()
                self.clean = 0
        else:
            self.clean = 0
            orch.congestion_reason = None

    async def run(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.step)
            except Exception:
                log.exception("monitor step failed")
            await asyncio.sleep(self.orch.settings.monitor_interval_s)
