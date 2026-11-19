import itertools

from app.htb_model import Flow, allocate

LINK, COMMERCIAL = 100, 80


def flows(embb=(50, 80, 0), urllc=(25, 80, 0), public=(20, 100, 0)):
    """Each tuple is (guarantee, ceiling, demand). Lower prio number = served first."""
    return [
        Flow("embb", embb[0], embb[1], 1, embb[2], True),
        Flow("urllc", urllc[0], urllc[1], 0, urllc[2], True),
        Flow("public", public[0], public[1], 2, public[2], False),
    ]


def test_busy_tenant_borrows_when_others_are_idle():
    got = allocate(LINK, COMMERCIAL, flows(embb=(50, 80, 80), urllc=(25, 80, 0), public=(20, 100, 0)))
    assert got["embb"] == 80  # 50 guaranteed + 30 borrowed from idle URLLC
    assert got["urllc"] == 0


def test_borrowed_bandwidth_is_returned_when_the_owner_wakes_up():
    got = allocate(LINK, COMMERCIAL, flows(embb=(50, 80, 80), urllc=(25, 80, 80)))
    assert got["urllc"] >= 25 and got["embb"] >= 50  # both guarantees honoured
    assert got["embb"] + got["urllc"] == COMMERCIAL


def test_urllc_wins_idle_bandwidth_over_embb():
    got = allocate(LINK, COMMERCIAL, flows(embb=(50, 80, 80), urllc=(25, 80, 80)))
    assert got["urllc"] == 30 and got["embb"] == 50  # URLLC took the 5 Mbit/s left in the 80 cap


def test_commercial_slices_can_never_touch_the_public_reserve():
    got = allocate(LINK, COMMERCIAL, flows(embb=(50, 80, 100), urllc=(25, 80, 100), public=(20, 100, 100)))
    assert got["embb"] + got["urllc"] <= COMMERCIAL
    assert got["public"] >= 20


def test_public_may_use_idle_commercial_bandwidth():
    got = allocate(LINK, COMMERCIAL, flows(embb=(50, 80, 0), urllc=(25, 80, 0), public=(20, 100, 100)))
    assert got["public"] == 100


def test_public_guarantee_holds_under_any_commercial_demand():
    for e, u in itertools.product((0, 10, 50, 80, 100), repeat=2):
        got = allocate(LINK, COMMERCIAL, flows(embb=(50, 80, e), urllc=(25, 80, u), public=(20, 100, 100)))
        assert got["public"] >= 20


def test_never_exceeds_link_ceiling_or_demand():
    for e, u, p in itertools.product((0, 5, 60, 100), repeat=3):
        fl = flows(embb=(50, 70, e), urllc=(25, 60, u), public=(20, 100, p))
        got = allocate(LINK, COMMERCIAL, fl)
        assert sum(got.values()) <= LINK + 1e-9
        assert got["embb"] + got["urllc"] <= COMMERCIAL + 1e-9
        for f in fl:
            assert got[f.name] <= min(f.demand, f.ceil) + 1e-9
