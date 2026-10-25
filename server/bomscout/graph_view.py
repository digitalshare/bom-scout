"""Read-only graph views: the subgraph the UI draws, blast radius of losing a part, and fixes already in the bin."""

from __future__ import annotations

from falkordb import Graph

from . import substitutes

SPEC_LABELS = ("Protocol", "Connector", "MountPattern", "RosDriver")


def _key(node) -> str:
    label = node.labels[0]
    p = node.properties
    return f"{label}:{p.get('sku') or p.get('id') or p.get('name')}"


def _label(node) -> str:
    p = node.properties
    kind = node.labels[0]
    if kind == "Part":
        return p.get("name", p["sku"]).replace("DYNAMIXEL ", "").replace("ROBOTIS ", "")
    if kind == "Offer":
        price = f"${p['price_usd']:.2f}" if p.get("price_usd") is not None else "price n/a"
        when = "today" if p.get("arrives_today") else (p.get("deliver_by") or "no ETA")
        return f"{price} · {when}"
    if kind == "Joint":
        return p["name"]
    if kind in ("Team", "Vendor", "Protocol"):
        return p.get("name") or p["id"]
    return p.get("id") or p.get("name") or kind


def default_joint(g: Graph, team: str) -> str | None:
    """The broken joint if there is one, else the joint with the highest static torque that has a part."""
    rows = g.query("""MATCH (:Team {id: $team})-[:HAS_BUILD]->()-[:HAS_JOINT]->(j:Joint)-[:USES]->(:Part)
                      RETURN j.name ORDER BY j.status = 'broken' DESC, j.static_torque_nm DESC LIMIT 1""",
                   {"team": team}).result_set
    return rows[0][0] if rows else None


def subgraph(g: Graph, team: str, joint: str) -> dict:
    """The build, the broken part and every candidate with the spec nodes that link them, what the team owns,
    and the live offers per vendor. Node status drives colour: broken / dropin / gaps / rejected / owned / today."""
    subs = substitutes.find(g, team, joint)
    cands = {c["sku"]: c for c in subs["candidates"]}
    skus = [subs["broken"], *cands]
    nodes: dict[int, object] = {}

    def add(*found):
        for n in found:
            if n is not None:
                nodes[n.id] = n

    for row in g.query("""MATCH (t:Team {id: $team})-[:HAS_BUILD]->(b:Build)-[:HAS_JOINT]->(j:Joint)-[:USES]->(p:Part)
                          RETURN t, b, j, p""", {"team": team}).result_set:
        add(*row)
    for row in g.query("""MATCH (p:Part) WHERE p.sku IN $skus
                          OPTIONAL MATCH (p)-[]->(s) WHERE s:Protocol OR s:Connector OR s:MountPattern OR s:RosDriver
                          RETURN p, s""", {"skus": skus}).result_set:
        add(*row)
    for row in g.query("MATCH (:Team {id: $team})-[:OWNS|POWERED_BY]->(x:Part) RETURN x", {"team": team}).result_set:
        add(*row)
    for row in g.query("""MATCH (o:Offer)-[:FOR]->(p:Part) WHERE p.sku IN $skus
                          MATCH (o)-[:FROM]->(v:Vendor) RETURN o, v""", {"skus": skus}).result_set:
        add(*row)

    owned = {sku: qty for sku, qty in g.query(
        "MATCH (:Team {id: $team})-[o:OWNS]->(p:Part) RETURN p.sku, o.qty", {"team": team}).result_set}
    constraints: dict[str, list[dict]] = {}
    for sku, agent, verdict, reason, source in g.query(
            """MATCH (:Team {id: $team})-[:HAS_BUILD]->()-[:HAS_JOINT]->(j:Joint {name: $joint})
               MATCH (c:Constraint)-[:AT]->(j), (c)-[:ON]->(p:Part)
               RETURN p.sku, c.agent, c.verdict, c.reason, c.source ORDER BY c.agent""",
            {"team": team, "joint": joint}).result_set:
        constraints.setdefault(sku, []).append({"agent": agent, "verdict": verdict, "reason": reason, "source": source})
    joint_broken = g.query("""MATCH (:Team {id: $team})-[:HAS_BUILD]->()-[:HAS_JOINT]->(j:Joint {name: $joint})
                              RETURN j.status = 'broken'""", {"team": team, "joint": joint}).result_set[0][0]
    broken_joints = {name for (name,) in g.query(
        "MATCH (:Team {id: $team})-[:HAS_BUILD]->()-[:HAS_JOINT]->(j:Joint {status: 'broken'}) RETURN j.name", {"team": team}).result_set}

    out_nodes = []
    for n in nodes.values():
        p, kind = n.properties, n.labels[0]
        node = {"id": _key(n), "label": _label(n), "kind": kind if kind != "Part" else p.get("category", "part"),
                "image_url": p.get("image_url"), "owned": owned.get(p.get("sku"), 0), "status": None, "detail": None}
        if kind == "Part" and p["sku"] == subs["broken"]:
            node["status"] = "broken" if joint_broken else "focus"  # the part on the joint being looked at
        elif kind == "Part" and p["sku"] in cands:
            c = cands[p["sku"]]
            node["status"] = "rejected" if not c["ok"] else ("gaps" if c["gaps"] else "dropin")
            node["detail"] = {"reasons": c["reasons"], "gaps": c["gaps"], "path": c["path"],
                              "constraints": constraints.get(p["sku"], [])}
        elif kind == "Joint" and p["name"] in broken_joints:
            node["status"] = "broken"
        elif kind == "Offer":
            node["status"] = "today" if p.get("arrives_today") else ("instock" if p.get("in_stock") else "later")
            node["detail"] = {"vendor": p["vendor"], "lead_time": p.get("lead_time"), "url": p.get("url"), "source": p.get("source")}
        elif node["owned"]:
            node["status"] = "owned"
        out_nodes.append(node)

    edges = [
        {"source": _key(nodes[a]), "target": _key(nodes[b]), "type": rel, "qty": props.get("qty")}
        for a, rel, b, props in g.query(
            """MATCH (a)-[r]->(b) WHERE ID(a) IN $ids AND ID(b) IN $ids
               RETURN ID(a), type(r), ID(b), properties(r)""", {"ids": list(nodes)}).result_set
    ]
    return {"joint": joint, "broken": f"Part:{subs['broken']}", "needed_nm": subs["needed_nm"],
            "nodes": out_nodes, "edges": edges}


def what_if_lost(g: Graph, team: str, sku: str) -> dict:
    """Which joints stop working if the team loses this part: joints that use it, every joint on the bus when it is
    the only owned adapter for the bus protocol, and every joint when it is the power supply."""
    hits: dict[str, str] = {}
    for (name,) in g.query("""MATCH (:Team {id: $team})-[:HAS_BUILD]->()-[:HAS_JOINT]->(j:Joint)-[:USES]->(:Part {sku: $sku})
                               RETURN j.name""", {"team": team, "sku": sku}).result_set:
        hits[name] = f"uses the {sku}"
    bus = g.query(
        """MATCH (t:Team {id: $team})-[o:OWNS]->(lost:Part {sku: $sku})-[:BRIDGES]->(p:Protocol)
           WHERE p.id = t.bus_protocol
           OPTIONAL MATCH (t)-[o2:OWNS]->(other:Part)-[:BRIDGES]->(p) WHERE other.sku <> $sku AND o2.qty > 0
           WITH t, p, o, count(other) AS others
           WHERE o.qty <= 1 AND others = 0
           MATCH (t)-[:HAS_BUILD]->()-[:HAS_JOINT]->(j:Joint)-[:USES]->(:Part)-[:SPEAKS]->(p)
           RETURN DISTINCT j.name, p.id""", {"team": team, "sku": sku}).result_set
    for name, proto in bus:
        hits.setdefault(name, f"only owned bridge from USB to the {proto} bus")
    for (name,) in g.query("""MATCH (t:Team {id: $team})-[:POWERED_BY]->(:Part {sku: $sku})
                               MATCH (t)-[:HAS_BUILD]->()-[:HAS_JOINT]->(j:Joint) RETURN j.name""",
                           {"team": team, "sku": sku}).result_set:
        hits.setdefault(name, "the only power supply")
    owned = g.query("MATCH (:Team {id: $team})-[o:OWNS]->(:Part {sku: $sku}) RETURN o.qty", {"team": team, "sku": sku}).result_set
    return {"sku": sku, "owned_qty": owned[0][0] if owned else 0,
            "joints": [{"joint": j, "why": why} for j, why in sorted(hits.items())]}


def owned_spares(g: Graph, team: str) -> dict:
    """For every joint, the parts already in the team's bin that can replace its actuator (passing every hard rule)."""
    joints = [name for (name,) in g.query(
        "MATCH (:Team {id: $team})-[:HAS_BUILD]->()-[:HAS_JOINT]->(j:Joint) RETURN j.name ORDER BY j.name", {"team": team}).result_set]
    out = []
    for joint in joints:
        r = substitutes.find(g, team, joint)
        spares = [{"sku": c["sku"], "name": c["name"], "owned_qty": c["owned_qty"], "gaps": c["gaps"]}
                  for c in r["candidates"] if c["ok"] and c["owned_qty"] > 0]
        out.append({"joint": joint, "uses": r["broken"], "spares": spares})
    return {"joints": out}
