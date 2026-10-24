"""Web API: `uv run uvicorn bomscout.api:app --port 8020`, then open http://localhost:8020.

BOMSCOUT_RUNTIME=trueforge runs the agent on TrueForge (see trueforge/setup.py); the default runs the local OpenAI loop.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from . import agent, graph_view, trueforge_runner
from .graph import connect

WEB = Path(__file__).resolve().parents[2] / "web"
TEAM = "team_scout"
RUNTIME = os.environ.get("BOMSCOUT_RUNTIME", "local")

app = FastAPI(title="BOM Scout")


class Ask(BaseModel):
    question: str
    live: bool = False


class Decision(BaseModel):
    approved: bool = True


def _sse(events: Iterator[dict]) -> StreamingResponse:
    def stream():
        try:
            for ev in events:
                yield f"data: {json.dumps(ev, default=str)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'text': f'{type(e).__name__}: {e}'})}\n\n"
        yield "data: {\"type\": \"done\"}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/team")
def team():
    tools = agent.Tools(connect(), TEAM, live=False)
    return {"runtime": RUNTIME, "team": tools.team_props, **tools.get_build(), **tools.team_inventory()}


@app.get("/graph")
def graph(joint: str = "elbow"):
    return graph_view.subgraph(connect(), TEAM, joint)


@app.get("/what-if/{sku}")
def what_if(sku: str):
    return graph_view.what_if_lost(connect(), TEAM, sku)


@app.get("/spares")
def spares():
    return graph_view.owned_spares(connect(), TEAM)


@app.post("/ask")
def ask(body: Ask):
    if RUNTIME == "trueforge":
        return _sse(trueforge_runner.run(body.question, TEAM, live=body.live))
    return _sse(agent.run(body.question, TEAM, live=body.live))


@app.post("/approve/{approval_id}")
def approve(approval_id: str, body: Decision):
    if RUNTIME == "trueforge":
        return _sse(trueforge_runner.decide(approval_id, body.approved, TEAM))

    def local():
        try:
            r = agent.approve(connect(), approval_id, approved=body.approved)
        except KeyError:
            yield {"type": "error", "text": f"no approval {approval_id}"}
            return
        yield {"type": "decision", "approval_id": approval_id, "status": r["status"]}
        if r.get("cart"):
            yield {"type": "tool_result", "name": "add_to_cart", "result": {"status": r["status"], "items": r["cart"]}}

    return _sse(local())
