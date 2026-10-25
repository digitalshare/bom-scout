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


def test_replace_build_swaps_joints_and_reset_restores_demo(g):
    import json

    from bomscout import sizing
    from bomscout.models import Build, Joint, Link

    catalog = json.loads((SAMPLES / "catalog.json").read_text())
    small = Build(id="gripper_bot", payload_kg=0.05, tip_link="finger",
                  links=[Link(name="base"), Link(name="finger", mass_kg=0.02, com=(0.02, 0, 0))],
                  joints=[Joint(name="pinch", parent="base", child="finger", origin=(0.05, 0, 0), axis=(0, 1, 0))])
    r = sizing.load_new_build(g, "team_scout", small, catalog)
    assert [row["joint"] for row in r["joints"]] == ["pinch"] and r["joints"][0]["sku"]
    names = {n for (n,) in g.query("MATCH (:Team {id:'team_scout'})-[:HAS_BUILD]->()-[:HAS_JOINT]->(j) RETURN j.name").result_set}
    assert names == {"pinch"}
    sizing.reset_demo(g, "team_scout", catalog)
    names = {n for (n,) in g.query("MATCH (:Team {id:'team_scout'})-[:HAS_BUILD]->()-[:HAS_JOINT]->(j) RETURN j.name").result_set}
    assert names == {"base_yaw", "shoulder", "elbow", "wrist_pitch"}
    assert g.query("MATCH (j:Joint {name:'elbow'}) RETURN j.status").result_set[0][0] == "broken"


def test_pit_crew_flags_the_torque_vs_cost_conflict_and_picks_the_bin_for_today(g):
    from bomscout import graph as graph_mod, pitcrew

    # offers as the cache would give them: XM430 fits the torque budget but costs $310, XC430 is $137.89
    graph_mod.upsert_offers(g, [
        {"sku": sku, "vendor": "robotis_us", "error": None, "offers": [{
            "sku": sku, "vendor": "robotis_us", "title": sku, "price_usd": price, "in_stock": True, "stock_qty": None,
            "lead_time": None, "deliver_by": "2099-01-01", "arrives_today": False, "url": None, "source": "cache",
            "fetched_at": "2026-10-24T14:00:00", "image_url": None}]}
        for sku, price in (("xm430-w350-t", 310.39), ("xc430-w150-t", 137.89), ("xl430-w250-t", 27.5))])
    r = pitcrew.review(g, "team_scout", "elbow")
    rows = {x["sku"]: x["verdicts"] for x in r["rows"]}
    assert all(v["verdict"] == "pass" for a, v in rows["xc430-w150-t"].items() if a != "budget")
    assert rows["xm430-w350-t"]["mechanical"]["verdict"] == "pass" and rows["xm430-w350-t"]["budget"]["verdict"] == "fail"
    assert any(c["sku"] == "xm430-w350-t" for c in r["conflicts"])
    assert rows["xl330-m288-t"]["electrical"]["verdict"] == "fail"
    assert r["today_pick"] == "ax-12a"  # the only option that clears every check and is already in the bin
    assert g.query("MATCH (c:Constraint {source: 'computed'}) RETURN count(c)").result_set[0][0] == 3 * len(r["rows"])
