"""Finds substitutes for a joint's actuator. One Cypher query per hop rule; Python combines them into reasons and gaps."""

from __future__ import annotations

from falkordb import Graph

from .models import Substitute

CONTEXT = """
MATCH (t:Team {id: $team})-[:HAS_BUILD]->(:Build)-[:HAS_JOINT]->(j:Joint {name: $joint})-[:USES]->(old:Part)
MATCH (t)-[:POWERED_BY]->(sup:Part)
RETURN old.sku, old.name, j.static_torque_nm, t.safety_factor, t.bus_protocol, sup.sku, sup.v_out
"""

CANDIDATES = """
MATCH (old:Part {sku: $old}), (c:Part)
WHERE c.sku <> old.sku AND c.category = old.category
OPTIONAL MATCH (:Team {id: $team})-[o:OWNS]->(c)
RETURN c.sku, c.name, c.v_min, c.v_max, c.stall_torque_nm, coalesce(o.qty, 0)
"""

# hop: protocols the candidate shares with the broken part
SHARED_PROTOCOL = """
MATCH (old:Part {sku: $old})-[:SPEAKS]->(p:Protocol)<-[:SPEAKS]-(c:Part)
RETURN c.sku, collect(p.id)
"""

# hop: adapters that bridge USB to a protocol the candidate speaks, and whether the team owns one
BRIDGE = """
MATCH (c:Part {category: 'servo'})-[:SPEAKS]->(p:Protocol)<-[:BRIDGES]-(a:Part)
OPTIONAL MATCH (:Team {id: $team})-[o:OWNS]->(a)
RETURN c.sku, p.id, a.sku, coalesce(o.qty, 0)
"""

# hop: connector match, or a cable/adapter the team owns that has both connectors
CONNECTOR = """
MATCH (old:Part {sku: $old})-[:HAS_CONNECTOR]->(oc:Connector), (c:Part {category: 'servo'})-[:HAS_CONNECTOR]->(cc:Connector)
WHERE c.sku <> old.sku AND c.category = old.category
OPTIONAL MATCH (:Team {id: $team})-[:OWNS]->(x:Part)-[:HAS_CONNECTOR]->(oc)
WHERE oc <> cc AND (x)-[:HAS_CONNECTOR]->(cc) AND x.category = 'cable'
RETURN c.sku, oc.id, cc.id, collect(x.sku)
"""

# hop: mount pattern match, or brackets for the candidate's mount (owned first)
MOUNT = """
MATCH (old:Part {sku: $old})-[:MOUNTS]->(om:MountPattern), (c:Part {category: 'servo'})-[:MOUNTS]->(cm:MountPattern)
WHERE c.sku <> old.sku AND c.category = old.category
OPTIONAL MATCH (b:Part)-[:FITS]->(cm)
OPTIONAL MATCH (:Team {id: $team})-[o:OWNS]->(b)
RETURN c.sku, om.id, cm.id, collect(DISTINCT [b.sku, coalesce(o.qty, 0)])
"""

DRIVER = """
MATCH (old:Part {sku: $old})-[:DRIVEN_BY]->(d:RosDriver)<-[:DRIVEN_BY]-(c:Part)
RETURN c.sku, collect(d.id)
"""


def _rows(g: Graph, q: str, **params) -> list[list]:
    return g.query(q, params).result_set


def find(g: Graph, team: str, joint: str) -> dict:
    ctx = _rows(g, CONTEXT, team=team, joint=joint)
    if not ctx:
        raise ValueError(f"no joint {joint!r} in team {team!r}'s build")
    old_sku, old_name, static_nm, sf, bus, supply_sku, v_supply = ctx[0]
    needed = round((static_nm or 0) * sf, 3)

    shared = {sku: set(protos) for sku, protos in _rows(g, SHARED_PROTOCOL, old=old_sku)}
    bridges: dict[str, list[tuple[str, str, int]]] = {}
    for sku, proto, adapter, owned in _rows(g, BRIDGE, team=team):
        bridges.setdefault(sku, []).append((proto, adapter, owned))
    connectors = {r[0]: r[1:] for r in _rows(g, CONNECTOR, team=team, old=old_sku)}
    mounts = {r[0]: r[1:] for r in _rows(g, MOUNT, team=team, old=old_sku)}
    drivers = {sku: d for sku, d in _rows(g, DRIVER, old=old_sku)}

    out = []
    for sku, name, v_min, v_max, stall_nm, owned_qty in _rows(g, CANDIDATES, team=team, old=old_sku):
        reasons, gaps, path, hard_fail = [], [], [old_name], False

        protos = shared.get(sku, set())
        owned_bridge = next((b for b in bridges.get(sku, []) if b[2] > 0 and (b[0] in protos or not protos)), None)
        if bus in protos:
            reasons.append(f"speaks {bus} like the {old_sku}, so it drops onto the same bus")
            path.append(bus)
        elif protos:
            p = sorted(protos)[0]
            reasons.append(f"shares {p} with the {old_sku}")
            gaps.append(f"switch every servo on the bus from {bus} to {p}")
            path.append(p)
        elif bridges.get(sku):
            proto, adapter, _ = owned_bridge or bridges[sku][0]
            path.append(proto)
            gaps.append(f"runs a second bus: {proto} via {adapter}" + ("" if owned_bridge else f" (buy {adapter})"))
        else:
            hard_fail = True
            gaps.append("no protocol path to the existing controller")
        if owned_bridge and owned_bridge[0] in path:
            reasons.append(f"you own a {owned_bridge[1]} that bridges {owned_bridge[0]}")
            path.append(owned_bridge[1])

        if sku in connectors:
            oc, cc, cables = connectors[sku]
            if oc == cc:
                reasons.append(f"same {cc} connector, existing cables fit")
            elif cables:
                reasons.append(f"you own a {cables[0]} cable for {oc} to {cc}")
                path.append(cables[0])
            else:
                gaps.append(f"needs a {oc} to {cc} cable")

        if sku in mounts:
            om, cm, brackets = mounts[sku]
            brackets = [(b, q) for b, q in brackets if b]
            owned_brackets = [b for b, q in brackets if q > 0]
            if om == cm:
                reasons.append(f"same {cm} mount, your FR12 brackets bolt straight on" if cm == "x430" else f"same {cm} mount")
                path.append(cm)
                if owned_brackets:
                    path.append(owned_brackets[0])
            elif owned_brackets:
                reasons.append(f"different mount ({cm}), but you own {owned_brackets[0]}")
                gaps.append(f"re-plan the link geometry for the {cm} frame")
            else:
                buy = brackets[0][0] if brackets else "a custom adapter plate"
                gaps.append(f"different mount ({cm} vs {om}): needs {buy}")

        if not (v_min or 0) <= v_supply <= (v_max or 0):
            hard_fail = True
            gaps.append(f"rated {v_min}-{v_max} V, your {supply_sku} supplies {v_supply} V")
        else:
            reasons.append(f"rated {v_min}-{v_max} V covers your {v_supply} V supply")

        if stall_nm is not None and stall_nm < needed:
            hard_fail = True
            gaps.append(f"stall torque {stall_nm} N·m < {needed} N·m needed at the {joint} ({sf}x safety)")
        elif stall_nm is not None:
            reasons.append(f"stall torque {stall_nm} N·m ≥ {needed} N·m needed at the {joint}")

        if drivers.get(sku):
            reasons.append(f"same ROS driver ({drivers[sku][0]})")
        else:
            gaps.append("different ROS driver")

        path.append(name)
        score = 100 - 15 * len(gaps) - (60 if hard_fail else 0) + (10 if owned_qty else 0)
        out.append(Substitute(sku=sku, name=name, score=score, ok=not hard_fail, reasons=reasons, gaps=gaps,
                              path=path, owned_qty=owned_qty))
    out.sort(key=lambda s: -s.score)
    return {"joint": joint, "broken": old_sku, "needed_nm": needed, "static_nm": static_nm,
            "candidates": [s.model_dump() for s in out]}
