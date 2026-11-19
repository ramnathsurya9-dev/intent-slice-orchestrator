"""Orchestrator: owns slice state and applies it to the kernel under the lock."""

from __future__ import annotations

import shutil
import subprocess
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime

from .config import SLICE_DEFS, Settings
from .policy_engine import INTENT_DEFAULT_SLICE, INTENT_PROFILES, evaluate
from .state_lock import MemoryStore, RedisStore
from .traffic_manager import ClassSpec, TcError, TrafficManager


class PolicyRejected(Exception):
    def __init__(self, message: str, detail: dict) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


@dataclass
class SliceState:
    name: str
    ip: str
    kind: str
    priority: int
    commercial: bool
    class_minor: int
    rate_mbps: int  # guaranteed
    ceiling_mbps: int  # may borrow idle bandwidth up to this
    latency_ms: int
    intent: str
    interface: str = ""  # host-side veth, for display


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        store: MemoryStore | RedisStore,
        traffic: TrafficManager,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.store = store
        self.traffic = traffic
        self.clock = clock
        self.slices: dict[str, SliceState] = {}
        for d in SLICE_DEFS:
            rate, ceil, lat, intent = self._initial(d.commercial)
            self.slices[d.name] = SliceState(
                d.name, d.ip, d.kind, d.priority, d.commercial, d.class_minor, rate, ceil, lat, intent
            )
        self.telemetry: dict[str, dict] = {}
        self.telemetry_source = "simulated" if settings.dry_run else "kernel"
        self.events: deque[dict] = deque(maxlen=60)
        self.throttled: dict[str, tuple[int, int]] = {}  # slice -> (rate, ceiling) before the throttle
        self.congestion_reason: str | None = None
        self.sim_congestion_until = 0.0
        self.started = False

    # ---------- helpers ----------
    def _initial(self, commercial: bool) -> tuple[int, int, int, str]:
        """(guaranteed, ceiling, latency, intent) a slice has at startup and after a reset."""
        if commercial:
            p = INTENT_PROFILES["idle"]
            return p.rate_mbps, p.rate_mbps, p.latency_ms, "idle"
        return self.settings.public_reserve_mbps, self.settings.public_ceiling_mbps, 0, "reserved"

    def log(self, kind: str, message: str, **extra) -> None:
        self.events.appendleft(
            {"time": datetime.now().strftime("%H:%M:%S"), "kind": kind, "message": message, **extra}
        )

    @property
    def commercial_allocated(self) -> int:
        return sum(s.rate_mbps for s in self.slices.values() if s.commercial)

    def _persist(self) -> None:
        self.store.save_slices({n: asdict(s) for n, s in self.slices.items()})

    @staticmethod
    def _spec(s: SliceState) -> ClassSpec:
        return ClassSpec(s.class_minor, s.ip, s.rate_mbps, s.ceiling_mbps, 2 - s.priority,
                         s.latency_ms, s.commercial)

    def _apply_state(self, s: SliceState) -> None:
        self.traffic.update_class(self.settings.shaping_iface, self._spec(s))

    # ---------- lifecycle ----------
    def start(self) -> None:
        """Discover veths, flush leftovers from a previous run, build the HTB tree."""
        for s in self.slices.values():
            s.interface = self.traffic.discover_interface(s.name)
        with self.store.lock():
            cfg = self.settings
            self.traffic.setup_tree(
                cfg.shaping_iface, cfg.total_capacity_mbps, cfg.commercial_budget_mbps,
                [self._spec(s) for s in self.slices.values()],
            )
            self._persist()
        self.started = True
        self.log("startup", "HTB tree built; public reserve protected")

    def stop(self) -> None:
        """Fail-safe: remove every rule we installed so nothing stays throttled."""
        try:
            self.traffic.clear(self.settings.shaping_iface)
        except (TcError, ValueError):
            pass
        self.started = False

    # ---------- intents ----------
    def _targets(self, intent: str, slice_name: str | None) -> list[str]:
        if slice_name is not None:
            s = self.slices.get(slice_name)
            if s is None:
                raise ValueError(f"unknown slice {slice_name!r}")
            if not s.commercial:
                raise ValueError(f"{slice_name} is the protected public reserve and cannot be re-profiled")
            return [slice_name]
        if intent == "idle":
            return [n for n, s in self.slices.items() if s.commercial]
        return [INTENT_DEFAULT_SLICE[intent]]

    def apply_intent(
        self,
        intent: str,
        slice_name: str | None = None,
        rate_mbps: int | None = None,
        latency_ms: int | None = None,
        on_exceed: str | None = None,
        ceiling_mbps: int | None = None,
    ) -> dict:
        if intent not in INTENT_PROFILES:
            raise ValueError(f"unknown intent {intent!r}")
        profile = INTENT_PROFILES[intent]
        mode = on_exceed or self.settings.on_exceed
        targets = self._targets(intent, slice_name)
        req = profile.rate_mbps if rate_mbps is None else rate_mbps
        lat = profile.latency_ms if latency_ms is None else latency_ms
        budget = self.settings.commercial_budget_mbps
        if ceiling_mbps is not None:
            if ceiling_mbps > budget:
                raise ValueError(f"ceiling_mbps {ceiling_mbps} is above the commercial budget {budget}")
            if ceiling_mbps < req:
                raise ValueError("ceiling_mbps must be at least the guaranteed rate")
        priorities = {n: s.priority for n, s in self.slices.items() if s.commercial}

        with self.store.lock():
            working = {n: s.rate_mbps for n, s in self.slices.items() if s.commercial}
            scaled: list[dict] = []
            for name in targets:
                d = evaluate(
                    working, name, req, budget=budget, priorities=priorities,
                    mode=mode, floor=self.settings.min_rate_mbps,
                )
                if not d.allowed:
                    self.log("rejected", f"{intent} on {name}: {d.reason}", slice=name)
                    raise PolicyRejected(
                        d.reason,
                        {
                            "code": "capacity_exceeded",
                            "message": d.reason,
                            "commercial_allocated_mbps": self.commercial_allocated,
                            "commercial_budget_mbps": budget,
                        },
                    )
                working = d.allocations
                for other, (old, new) in d.scaled.items():
                    scaled.append({"slice": other, "from_mbps": old, "to_mbps": new})

            if ceiling_mbps is not None:
                target_ceil = ceiling_mbps
            else:
                target_ceil = budget if profile.borrows else req
            target_ceil = max(target_ceil, req)

            changed = [n for n in working if n in targets or working[n] != self.slices[n].rate_mbps]
            old = {n: (self.slices[n].rate_mbps, self.slices[n].ceiling_mbps,
                       self.slices[n].latency_ms, self.slices[n].intent) for n in changed}
            try:
                for n in changed:
                    s = self.slices[n]
                    was_fixed = s.ceiling_mbps == s.rate_mbps  # not borrowing
                    s.rate_mbps = working[n]
                    if n in targets:
                        s.ceiling_mbps, s.latency_ms, s.intent = target_ceil, lat, intent
                    else:  # scaled down: keep its ceiling, but never below the new guarantee
                        s.ceiling_mbps = s.rate_mbps if was_fixed else max(s.ceiling_mbps, s.rate_mbps)
                    self._apply_state(s)
            except (TcError, ValueError):
                for n, (r, c, lt, i) in old.items():  # put memory state and kernel back
                    s = self.slices[n]
                    s.rate_mbps, s.ceiling_mbps, s.latency_ms, s.intent = r, c, lt, i
                    try:
                        self._apply_state(s)
                    except (TcError, ValueError):
                        pass
                raise
            for n in changed:
                self.throttled.pop(n, None)  # an explicit intent overrides an emergency throttle
            self._persist()

            changes = [
                {"slice": n, "rate_mbps": self.slices[n].rate_mbps,
                 "ceiling_mbps": self.slices[n].ceiling_mbps,
                 "latency_ms": self.slices[n].latency_ms, "intent": self.slices[n].intent}
                for n in changed
            ]
            msg = (f"{intent} applied to {', '.join(targets)} "
                   f"(guaranteed {req}, up to {target_ceil} Mbit/s when others are idle); "
                   f"guarantees {self.commercial_allocated}/{budget} Mbit/s")
            self.log("intent", msg, scaled=scaled)
            for sc in scaled:
                self.log("scaled", f"{sc['slice']} guarantee scaled down {sc['from_mbps']} -> {sc['to_mbps']} Mbit/s")
            return {
                "allowed": True, "intent": intent, "changes": changes, "scaled_down": scaled,
                "reason": msg, "commercial_allocated_mbps": self.commercial_allocated,
                "commercial_budget_mbps": budget,
            }

    def reset(self) -> dict:
        with self.store.lock():
            for s in self.slices.values():
                s.rate_mbps, s.ceiling_mbps, s.latency_ms, s.intent = self._initial(s.commercial)
                self._apply_state(s)
            self.throttled.clear()
            self.congestion_reason = None
            self.sim_congestion_until = 0.0
            self._persist()
        self.log("reset", "all slices reset to default profiles")
        return {"reset": True, "commercial_allocated_mbps": self.commercial_allocated}

    # ---------- congestion demo ----------
    def simulate_congestion(self) -> dict:
        """Push demand beyond the commercial budget so the safeguard has to act."""
        steps: list[dict] = []

        def attempt(label: str, **kw) -> None:
            try:
                r = self.apply_intent(**kw)
                steps.append({"step": label, "allowed": True, "detail": r["reason"]})
            except PolicyRejected as exc:
                steps.append({"step": label, "allowed": False, "detail": exc.message})

        attempt("eMBB starts a 4K stream (50 Mbit/s guaranteed)", intent="4k_stream", slice_name="embb_slice")
        attempt("URLLC starts cloud gaming (25 Mbit/s guaranteed)", intent="cloud_gaming",
                slice_name="urllc_slice")
        attempt("crowd surge: eMBB asks for 70 Mbit/s (reject policy)", intent="4k_stream",
                slice_name="embb_slice", rate_mbps=70, on_exceed="reject")
        attempt("crowd surge: URLLC asks for 60 Mbit/s (scale-down policy)", intent="cloud_gaming",
                slice_name="urllc_slice", rate_mbps=60, on_exceed="scale_down")
        flood = self._start_flood()
        return {"steps": steps, "flood": flood, "commercial_allocated_mbps": self.commercial_allocated,
                "commercial_budget_mbps": self.settings.commercial_budget_mbps}

    def _start_flood(self) -> str:
        if self.settings.dry_run:
            self.sim_congestion_until = self.clock() + 12
            self.log("congestion", "simulated public-slice congestion for 12 s (dry-run)")
            return "simulated"
        iperf = shutil.which("iperf3")
        if not iperf:
            self.log("congestion", "iperf3 not found; no traffic flood was started")
            return "unavailable"
        ip = self.slices["public_slice"].ip
        subprocess.Popen(
            [iperf, "-c", ip, "-u", "-b", "100M", "-t", "10"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.log("congestion", f"UDP flood started toward {ip} for 10 s")
        return "iperf3"

    # ---------- emergency throttle (called by the monitor) ----------
    def emergency_throttle(self, reason: str) -> bool:
        """Pin non-critical (lowest-priority commercial) traffic to its minimum, so it stops borrowing
        and frees bandwidth for the public slice. Returns True if anything changed."""
        em = self.settings.emergency_rate_mbps
        with self.store.lock():
            self.congestion_reason = reason
            low = min((s for s in self.slices.values() if s.commercial), key=lambda s: s.priority)
            if low.name in self.throttled or (low.rate_mbps <= em and low.ceiling_mbps <= em):
                return False
            self.throttled[low.name] = (low.rate_mbps, low.ceiling_mbps)
            low.rate_mbps = min(low.rate_mbps, em)
            low.ceiling_mbps = low.rate_mbps
            self._apply_state(low)
            self._persist()
        self.log("emergency", f"{low.name} pinned to {low.rate_mbps} Mbit/s (no borrowing): {reason}")
        return True

    def release_emergency(self) -> None:
        with self.store.lock():
            budget = self.settings.commercial_budget_mbps
            for name, (prev_rate, prev_ceil) in list(self.throttled.items()):
                s = self.slices[name]
                others = self.commercial_allocated - s.rate_mbps
                s.rate_mbps = min(prev_rate, budget - others)  # never break the 80% cap on release
                s.ceiling_mbps = max(s.rate_mbps, min(prev_ceil, budget))
                self._apply_state(s)
                del self.throttled[name]
            self.congestion_reason = None
            self._persist()
        self.log("recovered", "congestion cleared; throttled slices restored")

    # ---------- view ----------
    def snapshot(self) -> dict:
        cfg = self.settings
        return {
            "capacity_mbps": cfg.total_capacity_mbps,
            "commercial_budget_mbps": cfg.commercial_budget_mbps,
            "public_reserve_mbps": cfg.public_reserve_mbps,
            "commercial_allocated_mbps": self.commercial_allocated,
            "telemetry_source": self.telemetry_source,
            "congestion_reason": self.congestion_reason,
            "slices": [
                {**asdict(s), "throttled": s.name in self.throttled,
                 "telemetry": self.telemetry.get(s.name, {})}
                for s in self.slices.values()
            ],
            "events": list(self.events),
        }
