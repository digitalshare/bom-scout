"""Pit Crew: mechanical, electrical and budget reviews of the options for one joint, written to the shared graph.

Each discipline gives every candidate a verdict (pass / warn / fail) with a reason. A candidate that one discipline
passes and another fails is a conflict ("XM430: mechanical pass, budget fail"). The checks reuse the substitution
hops, the budget maths and the Offer nodes, so the three views can be compared against the same graph.
TrueForge subagents can add their own verdicts with record_constraint; those are kept beside the computed ones.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from falkordb import Graph

from . import substitutes

AGENTS = ("mechanical", "electrical", "budget")
RANK = {"pass": 0, "warn": 1, "fail": 2}


def _worst(checks: list[tuple[str, str]]) -> dict:
    """[(verdict, reason)] -> the worst verdict, with every reason at that level."""
    if not checks:
        return {"verdict": "pass", "reason": "no issues"}
    level = max(RANK[v] for v, _ in checks)
    verdict = next(v for v, r in RANK.items() if r == level)
    return {"verdict": verdict, "reason": "; ".join(r for v, r in checks if RANK[v] == level)}


def _best_offer(g: Graph, sku: str) -> dict | None:
    """Earliest arrival, then lowest price; offers marked out of stock count only when there is nothing else."""
    rows = g.query("""MATCH (o:Offer)-[:FOR]->(:Part {sku: $sku})
                      RETURN o.vendor, o.price_usd, o.deliver_by, o.arrives_today, o.source, o.in_stock""", {"sku": sku}).result_set
    offers = [{"vendor": v, "price_usd": p, "deliver_by": d, "arrives_today": t, "source": s, "backorder": i is False}
              for v, p, d, t, s, i in rows]
    offers = [o for o in offers if not o["backorder"]] or offers
    dated = [o for o in offers if o["deliver_by"]]
    if dated:
        return min(dated, key=lambda o: (o["deliver_by"], o["price_usd"] if o["price_usd"] is not None else 1e9))
    return min(offers, key=lambda o: o["price_usd"] if o["price_usd"] is not None else 1e9) if offers else None


def _mechanical(c: dict, torque: dict) -> dict:
    checks = []
    static, needed, stall = torque["static_torque_nm"] or 0, torque["needed_nm"], torque["stall_torque_nm"]
    if stall < needed:
        checks.append(("fail", f"stall {stall} N·m < {needed} N·m needed"))
    elif static and stall < needed * 1.5:
        checks.append(("warn", f"thin margin: stall {stall} N·m is {stall / static:.1f}x static"))
    else:
        checks.append(("pass", f"stall {stall} N·m, {stall / static:.1f}x static" if static else f"stall {stall} N·m, no gravity load"))
    checks += [("warn", gap) for gap in c["gaps"] if "mount" in gap]
    return _worst(checks)


def _electrical(c: dict, power: dict, old_current: float, new_current: float) -> dict:
    checks = []
    for gap in c["gaps"]:
        if " V, your " in gap or "no protocol path" in gap:
            checks.append(("fail", gap))
        elif "bus" in gap or "cable" in gap or "ROS driver" in gap:
            checks.append(("warn", gap))
    if not power["current_ok"] and new_current > old_current:
        checks.append(("warn", f"raises all-stall current to {power['all_stall_current_a']} A on a {power['max_current_a']} A supply"))
    return _worst(checks) if checks else {"verdict": "pass", "reason": f"same bus and supply ({power['v_out']} V)"}


def _budget(c: dict, offer: dict | None, cap: float, today: str, deadline: str) -> dict:
    if c["owned_qty"]:
        return {"verdict": "pass", "reason": f"already in your bin ({c['owned_qty']} owned), $0 and available now"}
    if offer is None:
        return {"verdict": "fail", "reason": "no listing found at any scraped vendor"}
    checks = []
    price = offer["price_usd"]
    if price is not None and price > cap:
        checks.append(("fail", f"${price:.2f} at {offer['vendor']} breaks the ${cap:.0f} cap"))
    if offer["arrives_today"] or offer["deliver_by"] == today:
        checks.append(("pass", f"arrives today from {offer['vendor']}"))
    elif offer["backorder"]:
        checks.append(("warn", f"backordered at {offer['vendor']}" + (f", arrives {offer['deliver_by']}" if offer["deliver_by"] else "")))
    elif offer["deliver_by"]:
        checks.append(("warn", f"arrives {offer['deliver_by']} from {offer['vendor']}, after today's {deadline} deadline"))
    else:
        checks.append(("fail", f"no delivery estimate from {offer['vendor']}"))
    if price is not None and price <= cap:
        checks.append(("pass", f"${price:.2f} fits the ${cap:.0f} cap"))
    return _worst(checks)


def review(g: Graph, team: str, joint: str, skus: list[str] | None = None) -> dict:
    """Runs the three reviews over every candidate for the joint (plus a like-for-like replacement), writes them as
    Constraint nodes, and returns the matrix, the conflicts and the recommendations."""
    from .agent import Tools  # agent.Tools exposes this review as a tool, so import it here, not at module level

    subs = substitutes.find(g, team, joint)
    old = subs["broken"]
    tools = Tools(g, team, live=False)
    tp = tools.team_props
    today = datetime.now(ZoneInfo(tp["tz"])).date().isoformat()
    like_for_like = {"sku": old, "name": subs["broken_name"], "ok": True, "gaps": [], "owned_qty": 0,
                     "reasons": ["the same part, like for like"]}
    candidates = [like_for_like] + [c for c in subs["candidates"]]
    if skus:
        candidates = [c for c in candidates if c["sku"] in skus]
    old_current = tools.parts[old]["stall_current_a"]

    rows = []
    for c in candidates:
        budget_check = tools.check_budget(joint, c["sku"])
        verdicts = {
            "mechanical": _mechanical(c, budget_check["torque"]),
            "electrical": _electrical(c, budget_check["power"], old_current, tools.parts[c["sku"]]["stall_current_a"]),
            "budget": _budget(c, _best_offer(g, c["sku"]), tp["budget_cap_usd"], today, tp["deadline_local"]),
        }
        offer = _best_offer(g, c["sku"])
        rows.append({"sku": c["sku"], "name": c["name"], "owned_qty": c["owned_qty"], "verdicts": verdicts,
                     "deliver_by": today if c["owned_qty"] else (offer or {}).get("deliver_by"),
                     "price_usd": 0.0 if c["owned_qty"] else (offer or {}).get("price_usd")})

    _write(g, team, joint, rows)
    conflicts = []
    for r in rows:
        passes = [a for a in AGENTS if r["verdicts"][a]["verdict"] == "pass"]
        fails = [a for a in AGENTS if r["verdicts"][a]["verdict"] == "fail"]
        if passes and fails:
            conflicts.append({"sku": r["sku"], "name": r["name"], "text": f"{r['name']}: " + ", ".join(
                f"{a} {r['verdicts'][a]['verdict']} ({r['verdicts'][a]['reason']})" for a in passes + fails)})

    viable = [r for r in rows if all(v["verdict"] != "fail" for v in r["verdicts"].values())]

    def warns(r):
        return sum(v["verdict"] == "warn" for v in r["verdicts"].values())

    recommended = min(viable, key=lambda r: (warns(r), r["deliver_by"] or "9999", r["price_usd"] or 0), default=None)
    today_options = [r for r in viable if r["deliver_by"] == today]
    today_pick = min(today_options, key=lambda r: (warns(r), r["price_usd"] or 0), default=None)
    return {"joint": joint, "replaces": old, "today": today, "deadline": tp["deadline_local"], "cap_usd": tp["budget_cap_usd"],
            "rows": rows, "conflicts": conflicts,
            "recommended": recommended and recommended["sku"], "today_pick": today_pick and today_pick["sku"],
            "recorded": recorded(g, team, joint)}


def _write(g: Graph, team: str, joint: str, rows: list[dict]) -> None:
    jid = g.query("MATCH (:Team {id: $team})-[:HAS_BUILD]->()-[:HAS_JOINT]->(j:Joint {name: $joint}) RETURN j.id",
                  {"team": team, "joint": joint}).result_set[0][0]
    g.query("MATCH (c:Constraint {source: 'computed'})-[:AT]->(:Joint {id: $jid}) DETACH DELETE c", {"jid": jid})
    for r in rows:
        for agent, v in r["verdicts"].items():
            g.query(
                """MATCH (p:Part {sku: $sku}), (j:Joint {id: $jid})
                   CREATE (c:Constraint {id: $id, agent: $agent, verdict: $verdict, reason: $reason, source: 'computed'})
                   CREATE (c)-[:ON]->(p), (c)-[:AT]->(j)""",
                {"sku": r["sku"], "jid": jid, "id": f"{jid}/{r['sku']}/{agent}", "agent": agent, **v},
            )


def record(g: Graph, team: str, agent: str, joint: str, sku: str, verdict: str, reason: str) -> dict:
    """A reviewer's own verdict (e.g. from a TrueForge subagent), kept beside the computed one."""
    if agent not in AGENTS or verdict not in RANK:
        raise ValueError(f"agent must be one of {AGENTS} and verdict one of {tuple(RANK)}")
    jid = g.query("MATCH (:Team {id: $team})-[:HAS_BUILD]->()-[:HAS_JOINT]->(j:Joint {name: $joint}) RETURN j.id",
                  {"team": team, "joint": joint}).result_set[0][0]
    g.query("""MATCH (c:Constraint {source: 'agent', agent: $agent})-[:ON]->(:Part {sku: $sku}), (c)-[:AT]->(:Joint {id: $jid})
               DETACH DELETE c""", {"agent": agent, "sku": sku, "jid": jid})
    g.query("""MATCH (p:Part {sku: $sku}), (j:Joint {id: $jid})
               CREATE (c:Constraint {id: $id, agent: $agent, verdict: $verdict, reason: $reason, source: 'agent'})
               CREATE (c)-[:ON]->(p), (c)-[:AT]->(j)""",
            {"sku": sku, "jid": jid, "id": f"{jid}/{sku}/{agent}/agent", "agent": agent, "verdict": verdict, "reason": reason})
    return {"recorded": {"agent": agent, "joint": joint, "sku": sku, "verdict": verdict, "reason": reason}}


def recorded(g: Graph, team: str, joint: str) -> list[dict]:
    rows = g.query("""MATCH (:Team {id: $team})-[:HAS_BUILD]->()-[:HAS_JOINT]->(j:Joint {name: $joint})
                      MATCH (c:Constraint {source: 'agent'})-[:AT]->(j), (c)-[:ON]->(p:Part)
                      RETURN p.sku, c.agent, c.verdict, c.reason ORDER BY p.sku, c.agent""",
                   {"team": team, "joint": joint}).result_set
    return [{"sku": s, "agent": a, "verdict": v, "reason": r} for s, a, v, r in rows]
