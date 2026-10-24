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

The `budget-maths` skill (`skills/budget-maths`) re-runs the torque and power maths in the Daytona sandbox. TrueForge loads skills only from git, so set `BOMSCOUT_SKILLS_REPO=https://github.com/<you>/<repo>` once it is pushed, then re-run setup.

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
- The `budget-maths` skill is not registered until the repo is on GitHub (see above).
- The venue address in `samples/hackathon_arm/team.json` is a placeholder (zip 94103).
- Stretch goal: Pit Crew multi-agent variant.
