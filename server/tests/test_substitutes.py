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


def test_subgraph_links_broken_part_to_dropin_through_shared_specs(g):
    from bomscout import graph_view

    sg = graph_view.subgraph(g, "team_scout", "elbow")
    edges = {(e["source"], e["type"], e["target"]) for e in sg["edges"]}
    assert ("Part:xl430-w250-t", "SPEAKS", "Protocol:dxl_p2") in edges
    assert ("Part:xc430-w150-t", "SPEAKS", "Protocol:dxl_p2") in edges
    assert ("Part:fr12-h101k", "FITS", "MountPattern:x430") in edges
    nodes = {n["id"]: n for n in sg["nodes"]}
    assert nodes["Part:xl430-w250-t"]["status"] == "broken" and nodes["Part:xc430-w150-t"]["status"] == "dropin"
    assert nodes["Part:u2d2"]["owned"] == 1 and nodes["Part:xl330-m288-t"]["status"] == "rejected"


def test_losing_the_only_u2d2_stops_every_joint(g):
    from bomscout import graph_view

    r = graph_view.what_if_lost(g, "team_scout", "u2d2")
    assert {j["joint"] for j in r["joints"]} == {"base_yaw", "shoulder", "elbow", "wrist_pitch"}
    assert graph_view.what_if_lost(g, "team_scout", "rpi5-8gb")["joints"] == []
