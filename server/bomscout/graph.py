"""Loads the parts catalog, a team's build and what the team owns into FalkorDB."""

from __future__ import annotations

import json
import os
from pathlib import Path

from falkordb import FalkorDB, Graph

from . import budget
from .inputs import load_build_dir
from .models import Build

GRAPH_NAME = "bomscout"
SPEC_FIELDS = ("v_min", "v_max", "stall_torque_nm", "stall_current_a", "mass_g", "v_out", "max_current_a")


def connect(graph_name: str = GRAPH_NAME) -> Graph:
    db = FalkorDB(host=os.environ.get("FALKORDB_HOST", "localhost"), port=int(os.environ.get("FALKORDB_PORT", 6379)))
    return db.select_graph(graph_name)


def reset(g: Graph) -> None:
    try:
        g.delete()
    except Exception:  # graph does not exist yet
        pass


def load_catalog(g: Graph, catalog: dict) -> None:
    for pid, p in catalog["protocols"].items():
        g.query("MERGE (n:Protocol {id: $id}) SET n.name = $name, n.bus = $bus", {"id": pid, **p})
    for cid, desc in catalog["connectors"].items():
        g.query("MERGE (n:Connector {id: $id}) SET n.description = $desc", {"id": cid, "desc": desc})
    for mid, desc in catalog["mounts"].items():
        g.query("MERGE (n:MountPattern {id: $id}) SET n.description = $desc", {"id": mid, "desc": desc})
    for did, d in catalog["ros_drivers"].items():
        g.query("MERGE (n:RosDriver {id: $id}) SET n.name = $name, n.repo = $repo", {"id": did, **d})
    for vid, v in catalog["vendors"].items():
        g.query(
            "MERGE (n:Vendor {id: $id}) SET n.name = $name, n.ships_from = $ships_from, n.fastest_ship_days = $days",
            {"id": vid, "name": v["name"], "ships_from": v["ships_from"], "days": v["fastest_ship_days"]},
        )
    for p in catalog["parts"]:
        props = {k: p.get(k) for k in SPEC_FIELDS}
        g.query(
            """MERGE (n:Part {sku: $sku})
               SET n.name = $name, n.category = $category, n.mfr = $mfr, n.query = $query, n += $props""",
            {"sku": p["sku"], "name": p["name"], "category": p["category"], "mfr": p["mfr"], "query": p["query"],
             "props": {k: v for k, v in props.items() if v is not None}},
        )
        for proto in p.get("protocols", []):
            g.query("MATCH (n:Part {sku: $sku}), (x:Protocol {id: $id}) MERGE (n)-[:SPEAKS]->(x)", {"sku": p["sku"], "id": proto})
        for proto in p.get("bridges", []):
            g.query("MATCH (n:Part {sku: $sku}), (x:Protocol {id: $id}) MERGE (n)-[:BRIDGES]->(x)", {"sku": p["sku"], "id": proto})
        for conn in ([p["connector"]] if p.get("connector") else []) + p.get("connectors", []):
            g.query("MATCH (n:Part {sku: $sku}), (x:Connector {id: $id}) MERGE (n)-[:HAS_CONNECTOR]->(x)", {"sku": p["sku"], "id": conn})
        if p.get("mount"):
            g.query("MATCH (n:Part {sku: $sku}), (x:MountPattern {id: $id}) MERGE (n)-[:MOUNTS]->(x)", {"sku": p["sku"], "id": p["mount"]})
        for mount in p.get("fits", []):
            g.query("MATCH (n:Part {sku: $sku}), (x:MountPattern {id: $id}) MERGE (n)-[:FITS]->(x)", {"sku": p["sku"], "id": mount})
        for drv in p.get("ros_drivers", []):
            g.query("MATCH (n:Part {sku: $sku}), (x:RosDriver {id: $id}) MERGE (n)-[:DRIVEN_BY]->(x)", {"sku": p["sku"], "id": drv})


def load_team(g: Graph, build: Build, team: dict) -> None:
    g.query(
        """MERGE (t:Team {id: $id})
           SET t.name = $name, t.budget_cap_usd = $cap, t.safety_factor = $sf, t.bus_protocol = $bus,
               t.venue = $venue, t.venue_zip = $zip, t.tz = $tz, t.deadline_local = $deadline
           MERGE (b:Build {id: $build}) SET b.payload_kg = $payload, b.tip_link = $tip
           MERGE (t)-[:HAS_BUILD]->(b)
           WITH t
           MATCH (s:Part {sku: $supply})
           MERGE (t)-[:POWERED_BY]->(s)""",
        {
            "id": team["id"], "name": team["name"], "cap": team["budget_cap_usd"], "sf": team["safety_factor"],
            "bus": team["bus_protocol"], "venue": team["venue"]["address"], "zip": team["venue"]["zip"],
            "tz": team["venue"]["tz"], "deadline": team["deadline_local"], "build": build.id,
            "payload": build.payload_kg, "tip": build.tip_link, "supply": team["supply_sku"],
        },
    )
    for item in team["owns"]:
        g.query(
            "MATCH (t:Team {id: $team}), (p:Part {sku: $sku}) MERGE (t)-[o:OWNS]->(p) SET o.qty = $qty",
            {"team": team["id"], "sku": item["sku"], "qty": item["qty"]},
        )
    torques = budget.joint_torques(build.model_dump()) if build.links else {}
    broken = {b["joint"] for b in team.get("broken", [])}
    for j in build.joints:
        if j.type == "fixed":
            continue
        g.query(
            """MATCH (b:Build {id: $build})
               MERGE (j:Joint {id: $id}) SET j.name = $name, j.static_torque_nm = $torque, j.status = $status
               MERGE (b)-[:HAS_JOINT]->(j)
               WITH j
               MATCH (p:Part {sku: $sku})
               MERGE (j)-[:USES]->(p)""",
            {"build": build.id, "id": f"{build.id}/{j.name}", "name": j.name, "torque": torques.get(j.name),
             "status": "broken" if j.name in broken else "ok", "sku": j.sku},
        )


def load(catalog_path: Path, build_dir: Path, graph_name: str = GRAPH_NAME) -> Graph:
    g = connect(graph_name)
    reset(g)
    load_catalog(g, json.loads(catalog_path.read_text()))
    build, team = load_build_dir(build_dir)
    load_team(g, build, team)
    return g
