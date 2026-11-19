from app.config import Settings
from app.main import build_orchestrator
from app.monitor import Monitor, evaluate_congestion
from app.traffic_manager import TrafficManager

from tests.conftest import FakeClock


def test_evaluate_congestion_thresholds():
    assert evaluate_congestion(0.0, 10, 100, 2.0, 95) is None
    assert evaluate_congestion(2.0, 10, 100, 2.0, 95) is None  # must EXCEED 2%
    assert "dropping" in evaluate_congestion(2.1, 10, 100, 2.0, 95)
    # a full link is normal now that idle bandwidth is lent out ...
    assert evaluate_congestion(0.0, 99, 100, 2.0, 95) is None
    # ... it only counts once the public slice is hurting
    assert "saturated" in evaluate_congestion(1.0, 96, 100, 2.0, 95)


def make():
    clock = FakeClock()
    orch = build_orchestrator(Settings(dry_run=True))
    orch.clock = clock
    orch.start()
    return orch, Monitor(orch), clock


def test_simulated_telemetry_shows_borrowing_then_giving_it_back():
    orch, mon, _ = make()
    orch.apply_intent("4k_stream")  # eMBB: 50 guaranteed, up to 80; URLLC idle
    mon.step()
    embb = orch.telemetry["embb_slice"]
    assert embb["tx_mbps"] > 70 and embb["borrowing_mbps"] > 20  # using idle capacity

    orch.apply_intent("cloud_gaming")  # URLLC wakes up and reclaims its guarantee
    mon.step()
    t = orch.telemetry
    assert t["urllc_slice"]["tx_mbps"] >= 25 and t["embb_slice"]["tx_mbps"] >= 50
    assert t["embb_slice"]["tx_mbps"] + t["urllc_slice"]["tx_mbps"] <= 80 + 1e-9


def test_self_healing_loop_throttles_then_releases():
    orch, mon, clock = make()
    orch.apply_intent("4k_stream")
    orch.simulate_congestion()  # eMBB guarantee ends at 20; public gets flooded (simulated)

    mon.step()
    emb = orch.slices["embb_slice"]
    assert emb.rate_mbps == 10 and emb.ceiling_mbps == 10 and "embb_slice" in orch.throttled
    assert "dropping" in orch.congestion_reason
    assert orch.slices["public_slice"].rate_mbps == 20  # protected reserve untouched

    clock.t += 30  # flood is over
    for _ in range(3):
        mon.step()
    assert not orch.throttled and orch.congestion_reason is None
    assert emb.rate_mbps == 20 and emb.ceiling_mbps == 80  # restored to the pre-throttle profile
    assert orch.commercial_allocated <= 80


def test_throttle_is_applied_to_the_kernel_and_logged():
    orch, mon, _ = make()
    orch.apply_intent("4k_stream")
    orch._start_flood()
    mon.step()
    cmds = [" ".join(c) for c in orch.traffic.runner.commands]
    assert any("classid 1:10 htb rate 10mbit ceil 10mbit" in c for c in cmds)
    assert any(e["kind"] == "emergency" for e in orch.events)


def test_release_never_breaks_the_80_percent_cap():
    orch, mon, clock = make()
    orch.apply_intent("4k_stream")
    orch._start_flood()
    mon.step()  # eMBB pinned to 10, previous guarantee 50
    orch.slices["urllc_slice"].rate_mbps = 60  # URLLC grew meanwhile (10 + 60 = 70)
    clock.t += 30
    for _ in range(3):
        mon.step()
    assert orch.commercial_allocated <= 80


def test_explicit_intent_overrides_a_throttle():
    orch, mon, _ = make()
    orch.apply_intent("4k_stream")
    orch._start_flood()
    mon.step()
    assert orch.throttled
    orch.apply_intent("idle", slice_name="embb_slice")
    assert not orch.throttled


def test_no_congestion_means_no_action():
    orch, mon, _ = make()
    orch.apply_intent("4k_stream")
    mon.step()
    assert not orch.throttled and orch.slices["embb_slice"].rate_mbps == 50
    assert set(orch.telemetry) == {"embb_slice", "urllc_slice", "public_slice"}


class CannedStatsRunner:
    """Returns pre-recorded `tc -s class show` output, one text per call."""

    def __init__(self, texts):
        self.texts = list(texts)

    def run(self, argv, check=True):
        return self.texts.pop(0) if "-s" in argv else ""


def tc_text(public_bytes, public_pkts, public_dropped):
    return (
        "class htb 1:10 parent 1:2 leaf 10: prio 1 rate 2Mbit ceil 2Mbit\n"
        " Sent 0 bytes 0 pkt (dropped 0, overlimits 0 requeues 0)\n"
        "class htb 1:20 parent 1:2 leaf 20: prio 0 rate 2Mbit ceil 2Mbit\n"
        " Sent 0 bytes 0 pkt (dropped 0, overlimits 0 requeues 0)\n"
        "class htb 1:30 parent 1:1 leaf 30: prio 2 rate 20Mbit ceil 100Mbit\n"
        f" Sent {public_bytes} bytes {public_pkts} pkt (dropped {public_dropped}, overlimits 0 requeues 0)\n"
        " backlog 0b 0p requeues 0\n"
    )


def test_real_mode_computes_rates_and_drops_from_htb_counters(monkeypatch):
    monkeypatch.setattr("app.monitor._ping_ms", lambda ip: 0.5)
    runner = CannedStatsRunner([tc_text(0, 0, 0), tc_text(12_500_000, 800, 200)])
    orch = build_orchestrator(Settings(dry_run=False))
    orch.traffic = TrafficManager(runner, verify_iface=False)
    clock = FakeClock()
    orch.clock = clock
    mon = Monitor(orch)

    mon.step()  # first sample: no rate yet
    assert orch.telemetry["public_slice"]["tx_mbps"] == 0.0
    clock.t += 1.0
    mon.step()
    pub = orch.telemetry["public_slice"]
    assert abs(pub["tx_mbps"] - 100.0) < 1e-6  # 12.5 MB in 1 s
    assert abs(pub["drop_pct"] - 20.0) < 1e-6  # 200 dropped vs 800 sent
    assert pub["borrowing_mbps"] == 80.0  # 100 sent against a 20 guarantee
    assert "dropping" in orch.congestion_reason  # and the monitor reacted


def test_monitor_survives_a_missing_htb_tree():
    orch = build_orchestrator(Settings(dry_run=False))
    orch.traffic = TrafficManager(CannedStatsRunner(["no such device"]), verify_iface=False)
    Monitor(orch).step()  # must not raise
    assert "error" in orch.telemetry["embb_slice"]
