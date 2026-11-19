"""Static slice definitions and environment-driven settings."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DASHBOARD_DIR = Path(__file__).resolve().parents[2] / "dashboard-ui"


@dataclass(frozen=True)
class SliceDef:
    name: str  # also the Docker container name
    ip: str
    kind: str
    priority: int  # higher = more critical (scaled down last, gets idle bandwidth first)
    commercial: bool  # counts against the 80% cap
    class_minor: int  # HTB class 1:<minor>


SLICE_DEFS = (
    SliceDef("embb_slice", "192.168.50.10", "eMBB", 1, True, 10),
    SliceDef("urllc_slice", "192.168.50.20", "URLLC", 2, True, 20),
    SliceDef("public_slice", "192.168.50.30", "public", 0, False, 30),
)


@dataclass(frozen=True)
class Settings:
    total_capacity_mbps: int = 100
    commercial_cap_pct: int = 80
    on_exceed: str = "reject"  # "reject" or "scale_down"
    min_rate_mbps: int = 2  # floor when scaling a slice down
    emergency_rate_mbps: int = 10  # eMBB guarantee and ceiling during an emergency throttle
    public_can_borrow: bool = True  # public may use idle commercial bandwidth (never the reverse)
    shaping_iface: str = "br-telecom"  # the Docker bridge the HTB tree is attached to
    redis_url: str = ""  # empty = in-memory store (dev/dry-run only)
    dry_run: bool = False  # no kernel changes, simulated telemetry
    monitor_interval_s: float = 2.0
    drop_threshold_pct: float = 2.0
    saturation_pct: float = 95.0
    release_after_clean: int = 3  # clean samples before an emergency throttle is lifted
    dashboard_dir: str = str(DEFAULT_DASHBOARD_DIR)
    sys_class_net: str = "/sys/class/net"

    @property
    def commercial_budget_mbps(self) -> int:
        return self.total_capacity_mbps * self.commercial_cap_pct // 100

    @property
    def public_reserve_mbps(self) -> int:
        return self.total_capacity_mbps - self.commercial_budget_mbps

    @property
    def public_ceiling_mbps(self) -> int:
        return self.total_capacity_mbps if self.public_can_borrow else self.public_reserve_mbps


def load_settings() -> Settings:
    env = os.environ.get
    return Settings(
        total_capacity_mbps=int(env("TOTAL_SYSTEM_CAPACITY_MBPS", "100")),
        on_exceed=env("POLICY_ON_EXCEED", "reject"),
        public_can_borrow=env("PUBLIC_CAN_BORROW", "1") == "1",
        shaping_iface=env("SHAPING_IFACE", "br-telecom"),
        redis_url=env("REDIS_URL", ""),
        dry_run=env("ORCH_DRY_RUN", "0") == "1",
        monitor_interval_s=float(env("MONITOR_INTERVAL_S", "2")),
        dashboard_dir=env("DASHBOARD_DIR", str(DEFAULT_DASHBOARD_DIR)),
    )
