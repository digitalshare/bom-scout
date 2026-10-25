"""MCP server that gives a TrueForge agent the parts graph, live offers, budget checks and the cart.

    uv run python -m bomscout.mcp_server     # streamable HTTP on 127.0.0.1:8771/mcp
"""

from __future__ import annotations

import os

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import budget, graph_view, pitcrew, sizing
from .agent import Tools
from .graph import connect

TEAM = os.environ.get("BOMSCOUT_TEAM", "team_scout")

# TrueForge asks for approval before tools marked write/destructive and runs read-only ones on their own.
# add_to_cart is also gated by name in trueforge/setup.py, since not every client honours the hints.
READ = ToolAnnotations(read_only_hint=True)
SCRAPE = ToolAnnotations(read_only_hint=True, open_world_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False)
SPEND = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)

server = MCPServer(
    name="bomscout",
    instructions=(
        "Tools over a robotics team's parts graph: their build, what they own, compatible substitutes with the graph "
        "path that proves each one, live vendor offers, and torque/power budgets. add_to_cart needs the team's approval."
    ),
)


def _tools(live: bool = False) -> Tools:
    return Tools(connect(), TEAM, live=live)


@server.tool(annotations=READ)
def get_build() -> dict:
    """The team's build: joints, the part on each joint, static gravity torque, and which joint is broken."""
    return _tools().get_build()


@server.tool(annotations=READ)
def team_inventory() -> dict:
    """Parts the team already owns, with quantities."""
    return _tools().team_inventory()


@server.tool(annotations=READ)
def find_substitutes(joint: str) -> dict:
    """Graph search for parts that can replace the actuator on a joint. Each candidate has the graph path
    (broken part -> shared protocol/adapter/mount/bracket -> substitute), hop-by-hop reasons, gaps, and owned quantity."""
    return _tools().find_substitutes(joint)


@server.tool(annotations=SCRAPE)
def get_offers(skus: list[str], live: bool = False) -> dict:
    """Price, stock and earliest delivery date to the venue for up to 4 catalog skus, from robotis.us, Digi-Key and
    Amazon via Bright Data. live=False replays the last scrape (instant); live=True scrapes now (about 90 s) and
    falls back to the last scrape per vendor on failure. Each result says source=live or cache."""
    return _tools(live).get_offers(skus[:4])


@server.tool(annotations=READ)
def check_budget(joint: str, sku: str) -> dict:
    """Torque check for a sku on a joint (static torque x safety factor vs stall torque) and the whole-arm power
    budget with that sku swapped in (all-stall current vs supply, supply voltage vs each actuator's range)."""
    return _tools().check_budget(joint, sku)


@server.tool(annotations=READ)
def get_budget_inputs(joint: str, sku: str) -> dict:
    """The raw build model (links, joints, payload) and actuator/supply specs, for re-running the budget maths in
    the sandbox with the budget-maths skill. Also returns this server's own answer for comparison."""
    from .inputs import load_build_dir
    from .agent import CATALOG

    t = _tools()
    build, _ = load_build_dir(CATALOG.parent / "hackathon_arm")
    joints = t.get_build()["joints"]
    actuators = [t.parts[sku if j["joint"] == joint else j["sku"]] for j in joints]
    supply_sku = t.g.query("MATCH (:Team {id: $id})-[:POWERED_BY]->(s:Part) RETURN s.sku", {"id": TEAM}).result_set[0][0]
    return {
        "build": build.model_dump(), "joint": joint, "sku": sku, "safety_factor": t.team_props["safety_factor"],
        "actuators": [{k: a[k] for k in ("sku", "v_min", "v_max", "stall_current_a", "stall_torque_nm")} for a in actuators],
        "supply": {k: t.parts[supply_sku][k] for k in ("sku", "v_out", "max_current_a")},
        "server_answer": {"joint_torques": budget.joint_torques(build.model_dump()), **t.check_budget(joint, sku)},
    }


@server.tool(annotations=WRITE)
def load_build(description: str) -> dict:
    """Replace the team's build with a new robot from a one-line description (e.g. "6-DOF arm, 2kg payload"):
    an LLM drafts links and joints, the budget maths gives each joint's torque, and each joint gets the cheapest
    catalog servo that clears it. Returns per-joint static/needed torque, chosen sku, price and why; unsized joints
    have no catalog part strong enough."""
    return _tools().load_build(description)


@server.tool(annotations=READ)
def pit_crew_review(joint: str, skus: list[str] | None = None) -> dict:
    """Mechanical, electrical and budget review of every option for a joint (plus a like-for-like replacement),
    computed from the graph: pass/warn/fail per discipline with reasons, conflicts (one passes, another fails), the
    recommended part, today's best option, and any verdicts the review subagents recorded. Pass skus to narrow it."""
    return _tools().pit_crew_review(joint, skus)


@server.tool(annotations=WRITE)
def record_constraint(agent: str, joint: str, sku: str, verdict: str, reason: str) -> dict:
    """A Pit Crew reviewer's verdict on one candidate. agent: mechanical | electrical | budget.
    verdict: pass | warn | fail. reason: one line citing the numbers. Stored in the graph beside the computed review."""
    return pitcrew.record(connect(), TEAM, agent, joint, sku, verdict, reason)


@server.tool(annotations=READ)
def what_if_lost(sku: str) -> dict:
    """Blast radius: which joints stop working if the team loses this part (joints that use it, every joint on the
    bus when it is the only owned USB adapter for the bus protocol, every joint when it is the power supply)."""
    return graph_view.what_if_lost(connect(), TEAM, sku)


@server.tool(annotations=READ)
def owned_spares() -> dict:
    """For every joint, parts already in the team's bin that can replace its actuator, with the gaps to close."""
    return graph_view.owned_spares(connect(), TEAM)


@server.tool(annotations=SPEND)
def add_to_cart(summary: str, items: list[dict]) -> dict:
    """NEEDS APPROVAL. Record the parts to buy as the team's cart. Nothing is ordered: the team checks out from each
    listing url. items: [{sku, vendor, qty, price_usd, deliver_by, url}] using values from get_offers only.
    summary: one paragraph on what to do today and what arrives when."""
    return _tools().add_to_cart(summary, items)


def main() -> None:
    server.run("streamable-http", host="127.0.0.1", port=int(os.environ.get("BOMSCOUT_MCP_PORT", 8771)))


if __name__ == "__main__":
    main()
