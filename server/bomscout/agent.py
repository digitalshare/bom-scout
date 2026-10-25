"""The BOM Scout agent: an OpenAI tool-calling loop over the graph, live offers and budget checks.

Every tool is a plain function on Tools, so a different agent runtime can wrap the same functions.
propose_cart never orders anything: it records a pending approval and ends the turn.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from falkordb import Graph
from openai import OpenAI

from . import budget, offers, sizing, substitutes
from .graph import connect, upsert_offers

DEFAULT_MODEL = os.environ.get("BOMSCOUT_MODEL", "gpt-5.5")
CATALOG = Path(os.environ.get("BOMSCOUT_CATALOG", Path(__file__).resolve().parents[2] / "samples" / "catalog.json"))

INSTRUCTIONS = """You are BOM Scout, a parts-sourcing agent for a robotics team at a hackathon or competition.
It is now {now} at the venue (zip {zip}). The team needs parts by {deadline} today. Budget cap: ${cap}.

How to work:
1. get_build to find the joint and part the user means. A joint with status "broken" is the likely one.
2. find_substitutes for that joint. Every candidate comes with the graph path and hop-by-hop reasons; cite them.
3. Check team_inventory: a part the team already owns beats anything that has to ship.
4. get_offers for the original part and the best substitutes (at most 4 skus). Offers marked source=cache are from an
   earlier scrape; say so. Never invent a price, stock level or delivery date that a tool did not return.
5. check_budget for the part you recommend (torque at that joint plus the whole power budget).
6. propose_cart with the parts to buy. It waits for the team's approval; you never order anything yourself.

For a new robot ("6-DOF arm, 2kg payload"), call load_build, then get_offers for the sized skus and propose_cart.
If a joint is unsized, say what torque it needs and that no catalog servo reaches it.

Answer the user's real question first: what can they have working by {deadline} today, and what arrives later.
If nothing arrives today, say so plainly and give the best same-day fallback from what they own, with its gaps.
Keep the final answer short: the plan, then the path, then the cart."""

TOOL_SPECS = [
    {"name": "get_build", "description": "The team's build: joints, the part on each joint, static torque, and which joint is broken.",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "team_inventory", "description": "Parts the team already owns, with quantities.",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "find_substitutes", "description": "Graph search for parts that can replace the actuator on a joint, with path, reasons and gaps.",
     "parameters": {"type": "object", "properties": {"joint": {"type": "string"}}, "required": ["joint"], "additionalProperties": False}},
    {"name": "get_offers", "description": "Live price, stock and earliest delivery date to the venue, per vendor, for catalog skus.",
     "parameters": {"type": "object", "properties": {"skus": {"type": "array", "items": {"type": "string"}, "maxItems": 4}},
                    "required": ["skus"], "additionalProperties": False}},
    {"name": "check_budget", "description": "Torque check for a sku on a joint, and the power budget with that sku swapped in.",
     "parameters": {"type": "object", "properties": {"joint": {"type": "string"}, "sku": {"type": "string"}},
                    "required": ["joint", "sku"], "additionalProperties": False}},
    {"name": "load_build", "description": "Replace the team's build with a new robot from a one-line description (e.g. '6-DOF arm, 2kg payload'), size an actuator for every joint, and return the sizing table.",
     "parameters": {"type": "object", "properties": {"description": {"type": "string"}}, "required": ["description"], "additionalProperties": False}},
    {"name": "propose_cart", "description": "Propose parts to buy. Creates a pending approval for the team; nothing is ordered.",
     "parameters": {"type": "object", "properties": {
         "summary": {"type": "string", "description": "one paragraph: what to do today, what arrives when"},
         "items": {"type": "array", "items": {"type": "object", "properties": {
             "sku": {"type": "string"}, "vendor": {"type": "string"}, "qty": {"type": "integer"},
             "price_usd": {"type": ["number", "null"]}, "deliver_by": {"type": ["string", "null"]}, "url": {"type": ["string", "null"]}},
             "required": ["sku", "vendor", "qty", "price_usd", "deliver_by", "url"], "additionalProperties": False}}},
         "required": ["summary", "items"], "additionalProperties": False}},
]


class Tools:
    def __init__(self, g: Graph, team: str, live: bool = True):
        self.g, self.team, self.live = g, team, live
        self.catalog = json.loads(CATALOG.read_text())
        self.parts = {p["sku"]: p for p in self.catalog["parts"]}
        row = g.query("MATCH (t:Team {id: $id}) RETURN t", {"id": team}).result_set[0][0]
        self.team_props = row.properties

    def get_build(self) -> dict:
        rows = self.g.query(
            """MATCH (t:Team {id: $team})-[:HAS_BUILD]->(b:Build)-[:HAS_JOINT]->(j:Joint)-[:USES]->(p:Part)
               RETURN b.id, j.name, j.status, j.static_torque_nm, p.sku, p.name, p.stall_torque_nm ORDER BY j.name""",
            {"team": self.team}).result_set
        return {"build": rows[0][0] if rows else None, "joints": [
            {"joint": r[1], "status": r[2], "static_torque_nm": r[3], "sku": r[4], "part": r[5], "stall_torque_nm": r[6]} for r in rows]}

    def team_inventory(self) -> dict:
        rows = self.g.query("MATCH (:Team {id: $team})-[o:OWNS]->(p:Part) RETURN p.sku, p.name, p.category, o.qty ORDER BY p.category",
                            {"team": self.team}).result_set
        return {"owns": [{"sku": r[0], "name": r[1], "category": r[2], "qty": r[3]} for r in rows]}

    def find_substitutes(self, joint: str) -> dict:
        return substitutes.find(self.g, self.team, joint)

    def get_offers(self, skus: list[str]) -> dict:
        parts = [{"sku": s, "name": self.parts[s]["name"], "query": self.parts[s]["query"]} for s in skus if s in self.parts]
        res = asyncio.run(offers.get_offers(parts, self.catalog["vendors"], self.team_props["venue_zip"], self.team_props["tz"], live=self.live))
        upsert_offers(self.g, res)
        return {"results": res, "unknown_skus": [s for s in skus if s not in self.parts]}

    def load_build(self, description: str) -> dict:
        from .inputs import describe_to_build

        return sizing.load_new_build(self.g, self.team, describe_to_build(description), self.catalog)

    def check_budget(self, joint: str, sku: str) -> dict:
        p = self.parts[sku]
        static = self.g.query("MATCH (:Team {id: $team})-[:HAS_BUILD]->()-[:HAS_JOINT]->(j:Joint {name: $joint}) RETURN j.static_torque_nm",
                              {"team": self.team, "joint": joint}).result_set[0][0]
        sf = self.team_props["safety_factor"]
        torque = {"joint": joint, "sku": sku, "static_torque_nm": static, "safety_factor": sf, "needed_nm": round(static * sf, 3),
                  "stall_torque_nm": p["stall_torque_nm"], "ok": p["stall_torque_nm"] >= static * sf,
                  "margin": round(p["stall_torque_nm"] / static, 2) if static else None}
        rows = self.g.query(
            """MATCH (t:Team {id: $team})-[:HAS_BUILD]->()-[:HAS_JOINT]->(j:Joint)-[:USES]->(p:Part)
               MATCH (t)-[:POWERED_BY]->(s:Part)
               RETURN j.name, p.sku, s.sku, s.v_out, s.max_current_a""", {"team": self.team}).result_set
        actuators = [self.parts[sku if name == joint else used] for name, used, *_ in rows]
        supply = {"sku": rows[0][2], "v_out": rows[0][3], "max_current_a": rows[0][4]}
        power = budget.check_power(actuators, supply)
        return {"torque": torque, "power": power}

    def propose_cart(self, summary: str, items: list[dict], status: str = "pending") -> dict:
        approval_id = uuid.uuid4().hex[:8]
        total = round(sum((i["price_usd"] or 0) * i["qty"] for i in items), 2)
        self.g.query(
            """MATCH (t:Team {id: $team})
               CREATE (a:Approval {id: $id, status: $status, summary: $summary, items: $items, total_usd: $total, created_at: $now})
               CREATE (t)-[:HAS_APPROVAL]->(a)""",
            {"team": self.team, "id": approval_id, "status": status, "summary": summary, "items": json.dumps(items),
             "total": total, "now": datetime.now(ZoneInfo(self.team_props["tz"])).isoformat()})
        return {"approval_id": approval_id, "status": status, "total_usd": total,
                "over_budget": total > self.team_props["budget_cap_usd"], "items": items, "summary": summary}

    def add_to_cart(self, summary: str, items: list[dict]) -> dict:
        """Under TrueForge the harness's approval gate runs before this call, so the cart is recorded as approved."""
        return self.propose_cart(summary, items, status="approved")

    def call(self, name: str, args: dict) -> dict:
        return getattr(self, name)(**args)


def approve(g: Graph, approval_id: str, approved: bool = True) -> dict:
    """The only path from a proposal to a cart. Builds vendor cart links; it never places an order."""
    rows = g.query("MATCH (a:Approval {id: $id}) RETURN a.status, a.items", {"id": approval_id}).result_set
    if not rows:
        raise KeyError(approval_id)
    if rows[0][0] != "pending":
        return {"approval_id": approval_id, "status": rows[0][0]}
    status = "approved" if approved else "rejected"
    g.query("MATCH (a:Approval {id: $id}) SET a.status = $status", {"id": approval_id, "status": status})
    return {"approval_id": approval_id, "status": status, "cart": json.loads(rows[0][1]) if approved else []}


def run(question: str, team: str = "team_scout", live: bool = True, g: Graph | None = None) -> Iterator[dict]:
    """Yields events: tool_call, tool_result, approval, answer."""
    g = g or connect()
    tools = Tools(g, team, live)
    tp = tools.team_props
    now = datetime.now(ZoneInfo(tp["tz"]))
    instructions = INSTRUCTIONS.format(now=now.strftime("%A %Y-%m-%d %H:%M"), zip=tp["venue_zip"],
                                       deadline=tp["deadline_local"], cap=tp["budget_cap_usd"])
    client = OpenAI()
    items: list = [{"role": "user", "content": question}]
    specs = [{"type": "function", "strict": False, **t} for t in TOOL_SPECS]
    for _ in range(12):
        resp = client.responses.create(model=DEFAULT_MODEL, instructions=instructions, input=items, tools=specs)
        items += resp.output
        calls = [o for o in resp.output if o.type == "function_call"]
        if not calls:
            yield {"type": "answer", "text": resp.output_text}
            return
        for c in calls:
            args = json.loads(c.arguments or "{}")
            yield {"type": "tool_call", "name": c.name, "args": args}
            try:
                result = tools.call(c.name, args)
            except Exception as e:
                result = {"error": f"{type(e).__name__}: {e}"}
            yield {"type": "tool_result", "name": c.name, "result": result}
            if c.name == "propose_cart" and "approval_id" in result:
                yield {"type": "approval", **result}
            items.append({"type": "function_call_output", "call_id": c.call_id, "output": json.dumps(result, default=str)})
    yield {"type": "answer", "text": "Stopped after 12 tool rounds without a final answer."}
