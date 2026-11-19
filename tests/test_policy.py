import itertools

import pytest
from app.policy_engine import evaluate

PRIO = {"embb_slice": 1, "urllc_slice": 2}
KW = {"budget": 80, "priorities": PRIO, "floor": 2}


def run(current, target, req, mode="reject"):
    return evaluate(current, target, req, mode=mode, **KW)


def test_within_budget_is_allowed():
    d = run({"embb_slice": 2, "urllc_slice": 2}, "embb_slice", 50)
    assert d.allowed and d.allocations == {"embb_slice": 50, "urllc_slice": 2}


def test_exactly_at_budget_is_allowed():
    assert run({"embb_slice": 50, "urllc_slice": 2}, "urllc_slice", 30).allowed


def test_one_over_budget_is_rejected_and_nothing_changes():
    cur = {"embb_slice": 50, "urllc_slice": 2}
    d = run(cur, "urllc_slice", 31)
    assert not d.allowed and d.allocations == cur


def test_request_larger_than_whole_budget_rejected_even_when_scaling():
    assert not run({"embb_slice": 2, "urllc_slice": 2}, "urllc_slice", 81, "scale_down").allowed


def test_scale_down_shrinks_lower_priority_slice():
    d = run({"embb_slice": 50, "urllc_slice": 25}, "urllc_slice", 60, "scale_down")
    assert d.allowed
    assert d.allocations == {"embb_slice": 20, "urllc_slice": 60}
    assert d.scaled == {"embb_slice": (50, 20)}


def test_never_scales_down_a_higher_priority_slice():
    cur = {"embb_slice": 20, "urllc_slice": 60}
    d = run(cur, "embb_slice", 50, "scale_down")
    assert not d.allowed and d.allocations == cur


def test_scale_down_respects_floor():
    # URLLC wants 79, eMBB can only be cut to the floor of 2 -> 79 + 2 = 81 > 80 -> reject
    d = run({"embb_slice": 50, "urllc_slice": 2}, "urllc_slice", 79, "scale_down")
    assert not d.allowed


def test_reducing_a_slice_is_always_allowed():
    assert run({"embb_slice": 50, "urllc_slice": 25}, "embb_slice", 2).allowed


def test_unknown_target_raises():
    with pytest.raises(KeyError):
        run({"embb_slice": 2}, "public_slice", 5)


@pytest.mark.parametrize("mode", ["reject", "scale_down"])
def test_invariant_commercial_total_never_exceeds_budget(mode):
    """Try every sequence of requests: the 80 Mbit/s cap must hold after each decision."""
    levels = [1, 2, 10, 25, 50, 60, 79, 80, 95]
    for first, second, third in itertools.product(levels, repeat=3):
        cur = {"embb_slice": 2, "urllc_slice": 2}
        for target, req in (("embb_slice", first), ("urllc_slice", second), ("embb_slice", third)):
            d = run(cur, target, req, mode)
            assert sum(d.allocations.values()) <= 80
            cur = d.allocations
