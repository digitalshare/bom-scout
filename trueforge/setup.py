"""Registers BOM Scout on a local TrueForge: OpenAI model, the bomscout MCP server, Daytona sandbox, skill and agent.

Idempotent (every call is a PUT or an upsert). Secrets come from the environment and are never written to disk:
OPENAI_API_KEY (required), DAYTONA_API_KEY, BOMSCOUT_SKILLS_REPO (https GitHub URL holding skills/budget-maths).

    uv run --project server python trueforge/setup.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx

TRUEFORGE = os.environ.get("TRUEFORGE_URL", "http://localhost:8790")
BOMSCOUT_MCP = os.environ.get("BOMSCOUT_MCP_URL", "http://127.0.0.1:8771/mcp")
MODEL = os.environ.get("BOMSCOUT_AGENT_MODEL", "gpt-5.5")
AGENT = "bom-scout"
ROOT = Path(__file__).resolve().parents[1]
INSTRUCTIONS = (ROOT / "trueforge" / "instructions.md").read_text()

api = httpx.Client(base_url=f"{TRUEFORGE}/api/v1", timeout=60)
if token := os.environ.get("TRUEFORGE_API_KEY"):
    api.headers["Authorization"] = f"Bearer {token}"


def call(method: str, path: str, body: dict | None = None) -> dict:
    response = api.request(method, path, json=body)
    if response.is_error:
        sys.exit(f"{method} {path} failed with {response.status_code}: {response.text[:500]}")
    return response.json() if response.content else {}


def model_name() -> str:
    """Reuses an OpenAI provider that already serves MODEL (e.g. one Showrunner set up); creates it otherwise."""
    configured = call("GET", "/settings/model-providers")["data"]
    for p in configured:
        if p["manifest"]["type"] == "openai" and any(m["model_id"] == MODEL for m in p["manifest"]["models"]):
            name = next(m["name"] for m in p["manifest"]["models"] if m["model_id"] == MODEL)
            print(f"model provider: reusing {p['name']}")
            return f"{p['name']}/{name}"
    catalog = call("GET", "/catalogs/model-providers")["data"]
    openai = next(p for p in catalog if p["type"] == "openai")
    models = [m for m in openai["models"] if m["model_id"] in (MODEL, "gpt-5.4-mini")]
    call("PUT", "/settings/model-providers", {"manifest": {"type": "openai", "auth": {"api_key": os.environ["OPENAI_API_KEY"]}, "models": models}})
    print(f"model provider: openai ({', '.join(m['model_id'] for m in models)})")
    return "openai/" + next(m["name"] for m in models if m["model_id"] == MODEL)


def mcp_server() -> list[dict]:
    manifest = {
        "type": "remote", "name": "bomscout", "url": BOMSCOUT_MCP,
        "description": "Robotics parts graph: build, inventory, substitutes with graph paths, Bright Data offers, budgets, cart.",
    }
    call("PUT", "/settings/mcp-servers", {"manifest": manifest})
    tools = call("GET", "/mcp-servers/bomscout/tools")
    names = [t.get("name") for t in tools.get("data", tools if isinstance(tools, list) else [])]
    print(f"mcp server: bomscout ({len(names)} tools: {', '.join(map(str, names))})")
    return [{"name": "bomscout", "enable_tools": ["@all"], "require_approval_for_tools": ["add_to_cart"], "preload": True}]


def sandbox() -> bool:
    if call("GET", "/settings/sandbox-providers").get("data"):
        print("sandbox: reusing configured provider")
        return True
    key = os.environ.get("DAYTONA_API_KEY")
    if not key:
        print("sandbox: skipped (DAYTONA_API_KEY not set)")
        return False
    call("PUT", "/settings/sandbox-providers", {"manifest": {
        "type": "daytona", "auth": {"api_key": key}, "exec_timeout_ms": 300_000,
        "auto_stop_interval_in_minutes": 15, "auto_archive_interval_in_minutes": 60, "auto_delete_interval_in_minutes": 120,
    }})
    print("sandbox: daytona")
    return True


def skills(sandbox_on: bool) -> list[dict]:
    repo = os.environ.get("BOMSCOUT_SKILLS_REPO")
    if not (repo and sandbox_on):
        print("skills: skipped (needs BOMSCOUT_SKILLS_REPO and the sandbox)")
        return []
    call("PUT", "/settings/skills", {"manifest": {
        "type": "git", "name": "budget-maths", "url": repo, "path": "skills/budget-maths",
        "ref": os.environ.get("BOMSCOUT_SKILLS_REF", "main"),
        "description": "Re-run a robot arm's torque and power budget in the sandbox to show the working behind a recommendation.",
    }})
    print("skills: budget-maths")
    return [{"name": "budget-maths"}]


def agent(model: str, servers: list[dict], skill_refs: list[dict]) -> None:
    body = {
        "name": AGENT,
        "description": "Parts-sourcing agent for robotics builds: graph substitutes, live offers, budget checks, approved cart.",
        "manifest": {
            "model": {"name": model, "params": {"reasoning_effort": "medium"}},
            "instructions": INSTRUCTIONS,
            "mcp_servers": servers,
            "skills": skill_refs,
            # the sandbox only earns its start-up time when there is a skill to run in it
            "config": {"sandbox": {"enabled": bool(skill_refs)}, "dynamic_sub_agents": {"enabled": True}},
        },
    }
    existing = next((a for a in call("GET", "/agents").get("data", []) if a.get("name") == AGENT), None)
    if existing:
        call("PUT", f"/agents/{existing['id']}", body)
        print(f"agent: updated {AGENT} ({existing['id']})")
    else:
        created = call("POST", "/agents", body)
        print(f"agent: created {AGENT} ({created.get('data', created).get('id')})")


if __name__ == "__main__":
    model = model_name()
    servers = mcp_server()
    agent(model, servers, skills(sandbox()))
    print(f"\nOpen {TRUEFORGE} and pick the '{AGENT}' agent, or run the BOM Scout UI with BOMSCOUT_RUNTIME=trueforge.")
