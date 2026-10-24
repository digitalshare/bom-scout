"""Needs FalkorDB on FALKORDB_HOST:FALKORDB_PORT. Uses its own graph so it never touches the demo graph."""

from pathlib import Path

import pytest

from bomscout import graph, substitutes

SAMPLES = Path(__file__).resolve().parents[2] / "samples"


@pytest.fixture(scope="module")
def g():
    try:
        g = graph.load(SAMPLES / "catalog.json", SAMPLES / "hackathon_arm", graph_name="bomscout_test")
    except Exception as e:  # FalkorDB not running
        pytest.skip(f"FalkorDB unavailable: {e}")
    yield g
    graph.reset(g)


def by_sku(result):
    return {c["sku"]: c for c in result["candidates"]}


def test_xc430_is_a_drop_in_for_the_elbow(g):
    r = substitutes.find(g, "team_scout", "elbow")
    xc = by_sku(r)["xc430-w150-t"]
    assert xc["ok"] and xc["gaps"] == []
    assert xc["path"] == ["DYNAMIXEL XL430-W250-T", "dxl_p2", "x430", "fr12-h101k", "DYNAMIXEL XC430-W150-T"]
    assert r["candidates"][0]["ok"]


def test_owned_ax12_works_with_gaps(g):
    ax = by_sku(substitutes.find(g, "team_scout", "elbow"))["ax-12a"]
    assert ax["ok"] and ax["owned_qty"] == 1
    assert "u2d2" in ax["path"] and "cable-x3p-molex" in ax["path"]
    assert any("dxl_p1" in gap for gap in ax["gaps"]) and any("fp04-f2" in gap for gap in ax["gaps"])


def test_hard_rules_reject_wrong_voltage_and_weak_servos(g):
    subs = by_sku(substitutes.find(g, "team_scout", "elbow"))
    assert not subs["xl330-m288-t"]["ok"]
    assert any("V" in gap for gap in subs["xl330-m288-t"]["gaps"])
    assert any("stall torque" in gap for gap in subs["xl330-m288-t"]["gaps"])
    assert not subs["sts3215"]["ok"]
