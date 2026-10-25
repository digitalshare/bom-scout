"""Web API: `uv run uvicorn bomscout.api:app --port 8020`, then open http://localhost:8020.

BOMSCOUT_RUNTIME=trueforge runs the agent on TrueForge (see trueforge/setup.py); the default runs the local OpenAI loop.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import tempfile

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from . import agent, graph_view, pitcrew, sizing, trueforge_runner
from .inputs import apply_bom, parse_bom, parse_urdf
from .graph import connect

WEB = Path(__file__).resolve().parents[2] / "web"
TEAM = "team_scout"
RUNTIME = os.environ.get("BOMSCOUT_RUNTIME", "local")

app = FastAPI(title="BOM Scout")


class Ask(BaseModel):
    question: str
    live: bool = False


class Describe(BaseModel):
    description: str


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
def graph(joint: str | None = None):
    g = connect()
    joint = joint or graph_view.default_joint(g, TEAM)
    if joint is None:
        return {"joint": None, "broken": None, "nodes": [], "edges": []}
    try:
        return graph_view.subgraph(g, TEAM, joint)
    except ValueError:  # the joint is from a build that has since been replaced
        return graph_view.subgraph(g, TEAM, graph_view.default_joint(g, TEAM))


@app.post("/pitcrew")
def pit_crew(joint: str | None = None):
    g = connect()
    return pitcrew.review(g, TEAM, joint or graph_view.default_joint(g, TEAM))


@app.get("/what-if/{sku}")
def what_if(sku: str):
    return graph_view.what_if_lost(connect(), TEAM, sku)


@app.get("/spares")
def spares():
    return graph_view.owned_spares(connect(), TEAM)


def _catalog() -> dict:
    return agent.Tools(connect(), TEAM, live=False).catalog


@app.post("/build/describe")
def build_describe(body: Describe):
    try:
        return agent.Tools(connect(), TEAM, live=False).load_build(body.description)
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.post("/build/upload")
async def build_upload(urdf: UploadFile = File(...), bom: UploadFile | None = File(None)):
    with tempfile.TemporaryDirectory() as tmp:
        urdf_path = Path(tmp) / "robot.urdf"
        urdf_path.write_bytes(await urdf.read())
        try:
            build = parse_urdf(urdf_path)
        except Exception as e:
            raise HTTPException(422, f"could not read the URDF: {e}")
        if bom is not None and bom.filename:
            bom_path = Path(tmp) / "bom.csv"
            bom_path.write_bytes(await bom.read())
            apply_bom(build, parse_bom(bom_path))
    try:
        return sizing.load_new_build(connect(), TEAM, build, _catalog())
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.post("/build/reset")
def build_reset():
    return sizing.reset_demo(connect(), TEAM, _catalog())


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
