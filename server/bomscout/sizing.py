"""Picks an actuator for every joint of a new build, and loads that build into the graph.

A joint keeps the actuator its BOM names. Otherwise it gets the cheapest catalog servo that clears the joint's
static torque x safety factor, runs on the team's supply voltage and speaks the team's bus protocol.
"""

from __future__ import annotations

import math
from pathlib import Path

from falkordb import Graph

from . import budget
from .graph import replace_build
from .inputs import load_build_dir
from .models import Build

DEMO_BUILD = Path(__file__).resolve().parents[2] / "samples" / "hackathon_arm"


def size_actuators(build: Build, servos: list[dict], supply_v: float, bus: str, safety_factor: float,
                   prices: dict[str, float]) -> list[dict]:
    """Fills joint.sku in place where the BOM left it empty; returns one sizing row per moving joint."""
    torques = budget.joint_torques(build.model_dump()) if build.links else {}
    rows = []
    for j in build.joints:
        if j.type == "fixed":
            continue
        static = torques.get(j.name, 0.0)
        needed = round(static * safety_factor, 3)
        row = {"joint": j.name, "static_nm": static, "needed_nm": needed, "sku": j.sku, "price_usd": prices.get(j.sku)}
        if j.sku:
            rows.append(row | {"why": "named in the BOM"})
            continue
        fits = [s for s in servos
                if s["stall_torque_nm"] >= needed and s["v_min"] <= supply_v <= s["v_max"] and bus in s.get("protocols", [])]
        if not fits:
            strongest = max((s for s in servos if s["v_min"] <= supply_v <= s["v_max"]), key=lambda s: s["stall_torque_nm"])
            rows.append(row | {"why": f"no catalog part strong enough: needs {needed} N·m, strongest at {supply_v} V is "
                                      f"{strongest['name']} ({strongest['stall_torque_nm']} N·m)"})
            continue

        def cost(s):
            return prices.get(s["sku"], s.get("list_price_usd", math.inf))

        best = min(fits, key=lambda s: (cost(s), -s["stall_torque_nm"]))
        j.sku = best["sku"]
        price = cost(best)
        rows.append(row | {"sku": best["sku"], "price_usd": None if price == math.inf else price,
                           "why": f"cheapest of {len(fits)} that clear {needed} N·m at {supply_v} V on {bus} "
                                  f"(stall {best['stall_torque_nm']} N·m, {best['stall_torque_nm'] / static:.1f}x static)"
                                  if static else f"cheapest of {len(fits)} on {bus} at {supply_v} V (no gravity load)"})
    return rows


def _team(g: Graph, team: str) -> dict:
    row = g.query("""MATCH (t:Team {id: $team})-[:POWERED_BY]->(s:Part)
                     RETURN t.safety_factor, t.bus_protocol, s.v_out""", {"team": team}).result_set[0]
    return {"safety_factor": row[0], "bus": row[1], "supply_v": row[2]}


def best_prices(g: Graph) -> dict[str, float]:
    """Lowest offer price per sku from the Offer nodes the last scrape wrote."""
    return {sku: price for sku, price in g.query(
        "MATCH (o:Offer)-[:FOR]->(p:Part) WHERE o.price_usd IS NOT NULL RETURN p.sku, min(o.price_usd)").result_set}


def load_new_build(g: Graph, team: str, build: Build, catalog: dict, broken: set[str] = frozenset()) -> dict:
    """Sizes the build's actuators, swaps it in as the team's build, and returns the sizing table."""
    try:
        budget.joint_torques(build.model_dump()) if build.links else {}
    except (KeyError, StopIteration) as e:
        raise ValueError(f"build {build.id!r} is not a connected joint/link tree: {e}") from e
    t = _team(g, team)
    servos = [p for p in catalog["parts"] if p["category"] == "servo"]
    rows = size_actuators(build, servos, t["supply_v"], t["bus"], t["safety_factor"], best_prices(g))
    replace_build(g, team, build, broken)
    return {"build": build.id, "payload_kg": build.payload_kg, "joints": rows,
            "total_usd": round(sum(r["price_usd"] or 0 for r in rows), 2),
            "unsized": [r["joint"] for r in rows if not r["sku"]]}


def reset_demo(g: Graph, team: str, catalog: dict) -> dict:
    build, team_file = load_build_dir(DEMO_BUILD)
    return load_new_build(g, team, build, catalog, broken={b["joint"] for b in team_file.get("broken", [])})
