import itertools


def stats(client):
    return client.get("/api/v1/stats").json()


def by_name(client):
    return {s["name"]: s for s in stats(client)["slices"]}


def rates(client):
    return {n: s["rate_mbps"] for n, s in by_name(client).items()}


def intent(client, **body):
    return client.post("/api/v1/intent", json=body)


def test_startup_state_and_public_reserve(client):
    s = stats(client)
    assert s["capacity_mbps"] == 100 and s["commercial_budget_mbps"] == 80
    assert s["public_reserve_mbps"] == 20
    assert rates(client) == {"embb_slice": 2, "urllc_slice": 2, "public_slice": 20}
    sl = by_name(client)
    assert sl["embb_slice"]["ceiling_mbps"] == 2  # idle: not borrowing
    assert sl["public_slice"]["ceiling_mbps"] == 100  # may use idle commercial bandwidth


def test_active_intents_get_a_guarantee_and_a_borrowing_ceiling(client):
    assert intent(client, intent="4k_stream").status_code == 200
    assert intent(client, intent="cloud_gaming").status_code == 200
    sl = by_name(client)
    assert sl["embb_slice"]["rate_mbps"] == 50 and sl["embb_slice"]["ceiling_mbps"] == 80
    assert sl["urllc_slice"]["rate_mbps"] == 25 and sl["urllc_slice"]["ceiling_mbps"] == 80
    assert sl["urllc_slice"]["latency_ms"] == 0 and sl["urllc_slice"]["intent"] == "cloud_gaming"
    assert stats(client)["commercial_allocated_mbps"] == 75


def test_pressing_the_same_intent_twice_changes_nothing(client):
    intent(client, intent="cloud_gaming")
    first = by_name(client)
    assert intent(client, intent="cloud_gaming").status_code == 200
    assert by_name(client) == first
    assert stats(client)["commercial_allocated_mbps"] == 27


def test_custom_ceiling(client):
    r = intent(client, intent="4k_stream", ceiling_mbps=60)
    assert r.status_code == 200
    assert by_name(client)["embb_slice"]["ceiling_mbps"] == 60


def test_bad_ceilings_are_422(client):
    assert intent(client, intent="4k_stream", ceiling_mbps=81).status_code == 422  # above 80 cap
    assert intent(client, intent="4k_stream", ceiling_mbps=40).status_code == 422  # below guarantee 50


def test_over_capacity_is_rejected_with_409_and_state_unchanged(client):
    intent(client, intent="4k_stream")
    intent(client, intent="cloud_gaming")
    before = by_name(client)
    r = intent(client, intent="4k_stream", rate_mbps=70)  # 70 + 25 > 80
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "capacity_exceeded"
    assert by_name(client) == before


def test_scale_down_policy_shrinks_lower_priority_guarantee(client):
    intent(client, intent="4k_stream")
    intent(client, intent="cloud_gaming")
    r = intent(client, intent="cloud_gaming", rate_mbps=60, on_exceed="scale_down")
    assert r.status_code == 200
    assert r.json()["scaled_down"] == [{"slice": "embb_slice", "from_mbps": 50, "to_mbps": 20}]
    assert rates(client) == {"embb_slice": 20, "urllc_slice": 60, "public_slice": 20}
    assert by_name(client)["embb_slice"]["ceiling_mbps"] >= 20


def test_public_reserve_is_never_touched(client):
    assert intent(client, intent="idle", slice="public_slice").status_code == 422
    intent(client, intent="4k_stream", rate_mbps=80)
    assert rates(client)["public_slice"] == 20


def test_guarantees_and_ceilings_never_break_the_cap_under_random_requests(client):
    for it, rate, mode, ceil in itertools.product(
        ("4k_stream", "cloud_gaming", "idle"), (None, 5, 40, 79, 95), ("reject", "scale_down"), (None, 80)
    ):
        intent(client, intent=it, rate_mbps=rate, on_exceed=mode, ceiling_mbps=ceil)
        assert stats(client)["commercial_allocated_mbps"] <= 80
        for s in stats(client)["slices"]:
            assert s["rate_mbps"] <= s["ceiling_mbps"]
            if s["commercial"]:
                assert s["ceiling_mbps"] <= 80  # commercial slices can never reach the public reserve


def test_idle_applies_to_all_commercial_slices(client):
    intent(client, intent="4k_stream")
    intent(client, intent="idle")
    sl = by_name(client)
    assert sl["embb_slice"]["rate_mbps"] == sl["embb_slice"]["ceiling_mbps"] == 2


def test_invalid_input_is_422(client):
    assert intent(client, intent="teleport").status_code == 422
    assert intent(client, intent="idle", rate_mbps=0).status_code == 422
    assert intent(client, intent="idle", latency_ms=5000).status_code == 422


def test_reset(client):
    intent(client, intent="4k_stream")
    assert client.post("/api/v1/reset").status_code == 200
    assert rates(client)["embb_slice"] == 2


def test_simulate_congestion_exercises_the_safeguard(client):
    r = client.post("/api/v1/congestion/simulate").json()
    assert [s["allowed"] for s in r["steps"]] == [True, True, False, True]
    assert r["commercial_allocated_mbps"] <= 80
    assert rates(client)["public_slice"] == 20


def test_htb_tree_is_built_updated_and_flushed(client):
    orch = client.app.state.orch
    runner = orch.traffic.runner
    start_cmds = [" ".join(c) for c in runner.commands]
    assert any("root handle 1: htb default 30" in c for c in start_cmds)
    assert sum("u32 match ip dst" in c for c in start_cmds) == 3

    intent(client, intent="cloud_gaming")
    cmds = [" ".join(c) for c in runner.commands]
    assert any("class replace dev br-telecom parent 1:2 classid 1:20 htb rate 25mbit ceil 80mbit prio 0" in c
               for c in cmds)

    n_before = len(runner.commands)
    orch.stop()
    flushed = [c for c in runner.commands[n_before:] if c[:3] == ["tc", "qdisc", "del"]]
    assert flushed == [["tc", "qdisc", "del", "dev", "br-telecom", "root"]]


def test_events_are_recorded(client):
    intent(client, intent="4k_stream", rate_mbps=95)
    kinds = [e["kind"] for e in stats(client)["events"]]
    assert "rejected" in kinds and "startup" in kinds


# ---- server-rendered dashboard ----
def test_dashboard_renders_python_side(client):
    r = client.get("/")
    assert r.status_code == 200
    for text in ("embb_slice", "urllc_slice", "public_slice", "Simulate 5G Crowd Congestion",
                 "Cloud Gaming", "Idle Power Save", "Reset All Slices", "protected", "guaranteed"):
        assert text in r.text


def test_dashboard_buttons_work_and_show_result(client):
    r = client.post("/ui/intent/cloud_gaming", follow_redirects=True)
    assert r.status_code == 200 and "cloud_gaming applied" in r.text
    assert rates(client)["urllc_slice"] == 25

    r = client.post("/ui/simulate", follow_redirects=True)
    assert "REFUSED" in r.text  # the safeguard refused the surge

    r = client.post("/ui/reset", follow_redirects=True)
    assert "reset" in r.text.lower() and rates(client)["urllc_slice"] == 2


def test_dashboard_escapes_flash_message(client):
    r = client.get("/", params={"msg": "<script>alert(1)</script>", "kind": "err"})
    assert "<script>alert(1)</script>" not in r.text


def test_dashboard_unknown_intent_is_reported_not_crashing(client):
    r = client.post("/ui/intent/teleport", follow_redirects=True)
    assert r.status_code == 200 and "unknown intent" in r.text
