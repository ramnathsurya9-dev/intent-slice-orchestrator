import pytest
from app.traffic_manager import (
    ClassSpec,
    DryRunRunner,
    TcError,
    TrafficManager,
    parse_class_stats,
)

SPECS = [
    ClassSpec(10, "192.168.50.10", 2, 2, 1, 50, True),
    ClassSpec(20, "192.168.50.20", 2, 2, 0, 0, True),
    ClassSpec(30, "192.168.50.30", 20, 100, 2, 0, False),
]


def manager(**kw):
    runner = DryRunRunner()
    kw.setdefault("verify_iface", False)
    return TrafficManager(runner, **kw), runner


def cmds(runner):
    return [" ".join(c) for c in runner.commands]


def test_setup_tree_builds_shared_htb_with_filters():
    tm, r = manager()
    tm.setup_tree("br-telecom", 100, 80, SPECS)
    c = cmds(r)
    assert c[0] == "tc qdisc del dev br-telecom root"
    assert c[1] == "tc qdisc add dev br-telecom root handle 1: htb default 30"
    assert "classid 1:1 htb rate 100mbit ceil 100mbit" in c[2]
    assert "parent 1:1 classid 1:2 htb rate 80mbit ceil 80mbit" in c[3]
    # commercial slices hang under the 80 Mbit/s class, public under the whole link
    assert "parent 1:2 classid 1:10 htb rate 2mbit ceil 2mbit prio 1" in " ".join(c)
    assert "parent 1:2 classid 1:20 htb rate 2mbit ceil 2mbit prio 0" in " ".join(c)
    assert "parent 1:1 classid 1:30 htb rate 20mbit ceil 100mbit prio 2" in " ".join(c)
    for ip, flow in (("192.168.50.10", "1:10"), ("192.168.50.20", "1:20"), ("192.168.50.30", "1:30")):
        assert any(f"match ip dst {ip}/32 flowid {flow}" in line for line in c)


def test_leaf_is_netem_only_when_latency_wanted():
    tm, r = manager()
    tm.setup_tree("br-telecom", 100, 80, SPECS)
    c = cmds(r)
    assert "tc qdisc replace dev br-telecom parent 1:10 handle 10: netem delay 50ms" in c
    assert "tc qdisc replace dev br-telecom parent 1:20 handle 20: pfifo" in c


def test_update_class_changes_only_that_class():
    tm, r = manager()
    tm.update_class("br-telecom", ClassSpec(20, "192.168.50.20", 25, 80, 0, 0, True))
    c = cmds(r)
    assert c[0].startswith("tc class replace dev br-telecom parent 1:2 classid 1:20 htb rate 25mbit ceil 80mbit")
    assert len(c) == 2 and "pfifo" in c[1]
    assert not any("qdisc del" in line or "filter" in line for line in c)


@pytest.mark.parametrize("iface", ["", "a b", "x;rm -rf", "waytoolonginterfacename", "$(id)"])
def test_bad_interface_names_are_rejected(iface):
    tm, r = manager()
    with pytest.raises(ValueError):
        tm.setup_tree(iface, 100, 80, SPECS)
    assert r.commands == []


@pytest.mark.parametrize(
    "spec",
    [
        ClassSpec(10, "192.168.50.10", 0, 10, 1, 0, True),  # rate < 1
        ClassSpec(10, "192.168.50.10", 10, 5, 1, 0, True),  # ceiling below guarantee
        ClassSpec(10, "192.168.50.10", 10, 20, 1, -1, True),  # negative latency
        ClassSpec(10, "not-an-ip", 10, 20, 1, 0, True),
        ClassSpec(10, "192.168.50.10", 10, 20, 9, 0, True),  # prio out of range
    ],
)
def test_bad_specs_are_rejected_before_any_command(spec):
    tm, r = manager()
    with pytest.raises(ValueError):
        tm.update_class("br-telecom", spec)
    assert r.commands == []


def test_setup_requires_exactly_one_public_class():
    tm, _ = manager()
    with pytest.raises(ValueError):
        tm.setup_tree("br-telecom", 100, 80, SPECS[:2])


def test_missing_bridge_is_reported(tmp_path):
    tm, r = manager(sys_class_net=str(tmp_path), verify_iface=True)
    with pytest.raises(TcError, match="not found"):
        tm.setup_tree("br-telecom", 100, 80, SPECS)
    assert r.commands == []


def test_parse_class_stats():
    out = (
        "class htb 1:1 root rate 100Mbit ceil 100Mbit burst 1600b cburst 1600b\n"
        " Sent 5000 bytes 50 pkt (dropped 0, overlimits 0 requeues 0)\n"
        " backlog 0b 0p requeues 0\n"
        "class htb 1:30 parent 1:1 leaf 30: prio 2 rate 20Mbit ceil 100Mbit\n"
        " Sent 123456 bytes 789 pkt (dropped 12, overlimits 34 requeues 0)\n"
        " backlog 4500b 3p requeues 0\n"
        "class htb 1:10 parent 1:2 leaf 10: prio 1 rate 2Mbit ceil 2Mbit\n"
        " Sent 999 bytes 9 pkt (dropped 0, overlimits 0 requeues 0)\n"
    )
    s = parse_class_stats(out)
    assert s[30] == {"sent_bytes": 123456, "sent_pkts": 789, "dropped": 12, "overlimits": 34,
                     "backlog_bytes": 4500}
    assert s[10]["sent_pkts"] == 9 and s[1]["sent_bytes"] == 5000
    assert parse_class_stats("") == {}


def test_discover_interface_matches_peer_index(tmp_path):
    for name, idx in (("lo", 1), ("eth0", 2), ("vethabc", 17)):
        d = tmp_path / name
        d.mkdir()
        (d / "ifindex").write_text(f"{idx}\n")
    tm, _ = manager(sys_class_net=str(tmp_path), container_exec=lambda c, argv: "17\n")
    assert tm.discover_interface("embb_slice") == "vethabc"


def test_discover_interface_fails_clearly(tmp_path):
    tm, _ = manager(sys_class_net=str(tmp_path), container_exec=lambda c, argv: "99\n")
    with pytest.raises(TcError):
        tm.discover_interface("embb_slice")
    tm2, _ = manager(sys_class_net=str(tmp_path), container_exec=lambda c, argv: "garbage")
    with pytest.raises(TcError):
        tm2.discover_interface("embb_slice")
