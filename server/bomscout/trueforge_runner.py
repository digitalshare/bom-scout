"""Runs the bom-scout agent on TrueForge and turns its turn events into the same events agent.run yields.

TrueForge owns the agent loop, subagents, sandbox and the approval gate on add_to_cart; this module only opens
sessions, streams turns and answers approvals. Set up the agent first with trueforge/setup.py.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator

from trueforge_sdk import TrueForge
from trueforge_sdk.events import is_event_delta, merge_event_delta

from .agent import Tools
from .graph import connect

TRUEFORGE = os.environ.get("TRUEFORGE_URL", "http://localhost:8790")
AGENT = os.environ.get("BOMSCOUT_TRUEFORGE_AGENT", "bom-scout")

# approval_id -> (session_id, thread_id, tool_call_id, (tool name, args)) for calls paused at the approval gate
_pending: dict[str, tuple[str, str, str, tuple[str, dict]]] = {}


def _client() -> TrueForge:
    return TrueForge(base_url=TRUEFORGE, token=os.environ.get("TRUEFORGE_TOKEN"), timeout=900)


def _parse(content):
    if not isinstance(content, str):
        return content
    try:
        return json.loads(content)
    except ValueError:
        return {"text": content}


def _stream(client: TrueForge, session_id: str, turn_input: list[dict], team: str,
            calls: dict[str, tuple[str, dict]] | None = None) -> Iterator[dict]:
    events: dict[str, object] = {}
    calls = dict(calls or {})  # tool_call_id -> (tool name, args); a resumed turn passes the paused call in

    def index_calls(msg) -> None:
        for tc in msg.tool_calls or []:
            name = tc.tool_info.name if tc.tool_info else tc.function.name
            calls[tc.id] = (name, _parse(tc.function.arguments or "{}"))

    for ev in client.sessions.create_turn_stream(session_id=session_id, input=turn_input):
        ev = getattr(ev, "data", ev)
        if is_event_delta(ev):
            if (base := events.get(ev.id)) is not None:
                merge_event_delta(base, ev)
            continue
        events[ev.id] = ev
        prefix = "" if ev.thread_id in (None, "main") else "subagent · "
        if ev.type == "thread.created":
            yield {"type": "step", "text": f"subagent started: {ev.title}"}
        elif ev.type == "sandbox.created":
            yield {"type": "step", "text": f"sandbox started ({ev.sandbox_id})"}
        elif ev.type == "tool.response":
            for msg in events.values():
                if getattr(msg, "type", None) == "model.message":
                    index_calls(msg)
            name, args = calls.get(ev.tool_call_id, ("tool", {}))
            yield {"type": "tool_call", "name": prefix + name, "args": args}
            yield {"type": "tool_result", "name": name, "result": _parse(ev.content)}
        elif ev.type == "tool.approval_required":
            for msg in events.values():
                if getattr(msg, "type", None) == "model.message":
                    index_calls(msg)
            for ref in ev.tool_calls:
                name, args = calls.get(ref.id, ("tool", {}))
                approval_id = ref.id
                _pending[approval_id] = (session_id, ev.thread_id, ref.id, (name, args))
                items = args.get("items", [])
                total = round(sum((i.get("price_usd") or 0) * i.get("qty", 1) for i in items), 2)
                cap = Tools(connect(), team, live=False).team_props["budget_cap_usd"]
                yield {"type": "tool_call", "name": f"{name} (waiting for approval)", "args": args}
                yield {"type": "approval", "approval_id": approval_id, "status": "pending", "summary": args.get("summary", ""),
                       "items": items, "total_usd": total, "over_budget": total > cap, "runtime": "trueforge"}
        elif ev.type == "mcp.auth_required":
            yield {"type": "error", "text": "an MCP server needs authorization in the TrueForge UI"}
        elif ev.type == "turn.done":
            state = ev.state
            if state.status == "done":
                if state.output is not None and state.output.content:
                    yield {"type": "answer", "text": state.output.content}
            else:
                yield {"type": "error", "text": f"turn {state.status}: {getattr(state, 'message', None) or getattr(state, 'reason', '')}"}


def run(question: str, team: str = "team_scout", live: bool = False) -> Iterator[dict]:
    client = _client()
    session = client.sessions.create(agent={"name": AGENT}, metadata={"team": team}).data
    yield {"type": "step", "text": f"TrueForge session {session.id} on agent {AGENT}"}
    hint = "\n\n(Fetch offers live: call get_offers with live=true.)" if live else ""
    yield from _stream(client, session.id, [{"type": "user.message", "content": question + hint}], team)


def decide(approval_id: str, approved: bool, team: str = "team_scout") -> Iterator[dict]:
    """Answers the approval gate, then streams the rest of the turn (add_to_cart runs only if allowed)."""
    if approval_id not in _pending:
        yield {"type": "error", "text": f"no pending approval {approval_id}"}
        return
    session_id, thread_id, call_id, call = _pending.pop(approval_id)
    decision = {"status": "allow"} if approved else {"status": "deny", "reason": "rejected by the team"}
    yield {"type": "decision", "approval_id": approval_id, "status": "approved" if approved else "rejected"}
    yield from _stream(_client(), session_id,
                       [{"type": "user.tool_approval", "thread_id": thread_id, "tool_call_id": call_id, "approval": decision}], team,
                       calls={call_id: call})
