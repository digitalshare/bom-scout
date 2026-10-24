# BOM Scout

A parts-sourcing agent for robotics builds. It finds substitutes in a FalkorDB compatibility graph, checks live price, stock and delivery through Bright Data, runs the torque and power budget, and asks for approval before building a cart.

## Run on TrueForge
TrueForge runs the agent loop, subagents, the Daytona sandbox, and the approval gate on `add_to_cart`. BOM Scout's tools reach it through an MCP server.
```
npx @truefoundry/trueforge@latest                              # :8790 (already running if Showrunner set it up)
cd server && uv run python -m bomscout.mcp_server              # bomscout MCP on 127.0.0.1:8771/mcp
uv run python ../trueforge/setup.py                            # model, MCP server, sandbox, skill, agent "bom-scout"
BOMSCOUT_RUNTIME=trueforge uv run uvicorn bomscout.api:app --port 8020
uv run python -m bomscout ask --trueforge "..."                # CLI; asks y/N at the approval gate
```
You can also open http://localhost:8790 and chat with the `bom-scout` agent directly.

The `budget-maths` skill (`skills/budget-maths`) re-runs the torque and power maths in the Daytona sandbox. TrueForge loads skills from git: run setup with `BOMSCOUT_SKILLS_REPO=https://github.com/digitalshare/bom-scout`.

## Run locally (no TrueForge)
```
# FalkorDB: reuses the container from ../1024 if it is already on :6379, otherwise
docker compose up -d falkordb

cd server
uv sync
uv run python -m bomscout ingest ../samples/hackathon_arm
uv run python -m bomscout subs elbow                       # graph substitution paths
uv run python -m bomscout offers xl430-w250-t --cache-only # offers from cache/offers
uv run python -m bomscout ask "The elbow servo burned out at 2pm. What can I get delivered to the venue today?" [--live]
uv run uvicorn bomscout.api:app --port 8020                # web UI on http://localhost:8020
uv run pytest
```
Env: `OPENAI_API_KEY`, `BRIGHTDATA_API_TOKEN`, `BRIGHTDATA_ZONE` (default `mcp_unlocker`), `BOMSCOUT_MODEL` (default `gpt-5.5`).

## 90-second demo
Before going on stage: `uv run python -m bomscout prewarm` (fresh offers and photos), then start the MCP server and the UI on the TrueForge runtime.
1. **Header** shows team, venue 94107, deadline 17:30, budget $150 and the broken elbow. "It's 2pm, the elbow servo just burned out."
2. **Find parts.** The steps log streams the TrueForge agent's tool calls. Point at the **graph panel**: the broken XL430 (red ring), the candidates and the parts you own (blue). Click **XC430**: the orange path runs XL430 → Protocol 2.0 → X430 mount → your FR12 bracket → XC430 → its vendor offers.
3. **Live offers (Bright Data)**: real prices and per-ZIP delivery dates. Nothing Dynamixel arrives today, and the agent says so.
4. **Budget check**: torque passes at 3.6×, and all-stall current is a warning on the 5 A supply. The sandbox step re-runs the maths in Daytona with the `budget-maths` skill.
5. **Approval**: the agent proposes today's fallback (the AX-12A you own, with its gaps) plus the XL430 arriving Tuesday. **Approve cart** resumes the TrueForge turn, and `add_to_cart` runs only now.
6. Encore: **What if we lose → U2D2** lights up the chain that takes down all four joints.

**If the network dies:** leave "Scrape vendors live" unticked. Offers come from the last scrape, marked `cache`, and a failed live scrape also falls back per vendor. The graph, budget and approval need no internet. The agent itself needs OpenAI.

## What the graph shows
- **Web UI graph panel** (`GET /graph?joint=elbow`, Cytoscape). It shows the build, the broken part, every candidate and the protocol, connector, mount and driver nodes that link them. Parts you own get a blue ring, rejected candidates are faded, and live offers hang off their vendor. Click a candidate to highlight its path. "What if we lose…" (`GET /what-if/{sku}`) lights up the failure chain. For example, the only U2D2 → Protocol 2.0 → every XL430 joint.
- **Part photos** are hotlinked from the vendor listings (robotis.us, Digi-Key, Amazon) and kept on `Part.image_url`. No image files are in this repo.
- **The pitch path in one query** (FalkorDB browser on :3000):
  ```cypher
  MATCH (b:Part {sku:"xl430-w250-t"})-[:SPEAKS]->(pr:Protocol)<-[:SPEAKS]-(s:Part)-[:MOUNTS]->(m)<-[:FITS]-(br:Part)<-[:OWNS]-(:Team),
        (o:Offer)-[:FOR]->(s), (o)-[:FROM]->(v:Vendor)
  WHERE s <> b AND pr.id = "dxl_p2"
  RETURN s.sku, m.id, br.sku, v.name, o.price_usd, o.deliver_by
  ```
- **Agent tools**: `what_if_lost(sku)` and `owned_spares()`, alongside the substitute search.

## Vendors (as tested 2026-10-24)
| Vendor | How | Status |
|---|---|---|
| robotis.us | Web Unlocker markdown, parsed by OpenAI | works; search pages show price and lead-time notes |
| Digi-Key | Web Unlocker markdown, parsed by OpenAI | works; marketplace stock and ship time; no XL430 listing |
| Amazon | Bright Data Amazon dataset, `discover_by_keyword` + venue zip | works; real per-zip delivery dates; only XM/XH/AX listed, not XL430/XC430 |
| Mouser | Web Unlocker | returns an empty page, so it is off by default |
| RobotShop | Web Unlocker | blocked until Bright Data KYC |

Every scrape is written to `cache/offers/<vendor>/<sku>.json`. If a live fetch fails, the last good result is served and tagged `cache`.

## Not done yet
- The venue address in `samples/hackathon_arm/team.json` is a placeholder (zip 94103).
- Stretch goal: Pit Crew multi-agent variant.
