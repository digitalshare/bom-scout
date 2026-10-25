"""CLI: `python -m bomscout ingest <build_dir>`, `subs <joint>`, `offers <sku>...`, `ask "<question>" [--live]`, `approve <id>`."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import agent, substitutes, trueforge_runner
from .graph import connect, load

ROOT = Path(__file__).resolve().parents[2]


def _print_events(events, trueforge: bool) -> str | None:
    """Prints agent events; returns the id of an approval still waiting for an answer."""
    pending = None
    for ev in events:
        if ev["type"] == "tool_call":
            print(f"→ {ev['name']}({json.dumps(ev['args'])[:300]})")
        elif ev["type"] == "step":
            print(f"· {ev['text']}")
        elif ev["type"] == "approval":
            pending = ev["approval_id"]
            hint = "" if trueforge else f"  (run `python -m bomscout approve {pending}`)"
            print(f"\n⏸ approval {pending}: ${ev['total_usd']}{hint}")
        elif ev["type"] == "decision":
            print(f"✔ cart {ev['status']}")
        elif ev["type"] == "error":
            print(f"✖ {ev['text']}")
        elif ev["type"] == "answer":
            print("\n" + ev["text"])
    return pending


def prewarm(team: str, batch: int = 4, live: bool = True, quiet: bool = False) -> list[dict]:
    """Live offers for every servo, adapter and bracket in the catalog, 4 skus at a time (Tools.get_offers also
    writes the Offer nodes and part photos to the graph). Prints which vendor answered live, from cache, or failed."""
    tools = agent.Tools(connect(), team, live=live)
    skus = [p["sku"] for p in tools.catalog["parts"] if p["category"] in ("servo", "adapter", "bracket")]
    results = []
    for i in range(0, len(skus), batch):
        results += tools.get_offers(skus[i:i + batch])["results"]
        if not quiet:
            print(f"fetched {min(i + batch, len(skus))}/{len(skus)}", flush=True)
    if quiet:
        return results
    vendors = sorted({r["vendor"] for r in results})
    print(f"\n{'sku':<16}" + "".join(f"{v:<22}" for v in vendors))
    for sku in skus:
        cells = []
        for v in vendors:
            r = next(r for r in results if r["sku"] == sku and r["vendor"] == v)
            best = min((o["price_usd"] for o in r["offers"] if o["price_usd"] is not None), default=None)
            cells.append(f"{r['source']}{' ERR' if r['error'] else ''}{f' ${best:.2f}' if best is not None else ' -'}")
        print(f"{sku:<16}" + "".join(f"{c:<22}" for c in cells))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(prog="bomscout")
    parser.add_argument("--team", default="team_scout")
    sub = parser.add_subparsers(dest="cmd", required=True)
    ingest = sub.add_parser("ingest", help="load the catalog and a build folder into FalkorDB")
    ingest.add_argument("build_dir", type=Path)
    ingest.add_argument("--catalog", type=Path, default=ROOT / "samples" / "catalog.json")
    subs = sub.add_parser("subs", help="substitutes for a joint's actuator")
    subs.add_argument("joint")
    off = sub.add_parser("offers", help="fetch offers for skus (live, falling back to cache)")
    off.add_argument("skus", nargs="+")
    off.add_argument("--cache-only", action="store_true")
    sub.add_parser("prewarm", help="live-scrape offers for every servo, adapter and bracket so the demo cache is fresh")
    ask = sub.add_parser("ask", help="ask the agent")
    ask.add_argument("question")
    ask.add_argument("--live", action="store_true", help="scrape vendors now instead of replaying the cache")
    ask.add_argument("--trueforge", action="store_true", help="run on the TrueForge bom-scout agent (approve in the prompt)")
    appr = sub.add_parser("approve", help="approve (or --reject) a proposed cart")
    appr.add_argument("approval_id")
    appr.add_argument("--reject", action="store_true")
    args = parser.parse_args()

    if args.cmd == "ingest":
        g = load(args.catalog, args.build_dir)
        prewarm(args.team, live=False, quiet=True)  # the reset dropped the Offer nodes; restore them from the cache
        print("loaded", g.name, dict(g.query("MATCH (n) RETURN labels(n)[0] AS label, count(n) ORDER BY label").result_set))
    elif args.cmd == "subs":
        r = substitutes.find(connect(), args.team, args.joint)
        print(f"{r['broken']} at {r['joint']}: needs {r['needed_nm']} N·m")
        for c in r["candidates"]:
            print(f"{'✔' if c['ok'] else '✖'} {c['score']:>5} {c['name']}  ({' → '.join(c['path'])})")
            for reason in c["reasons"]:
                print(f"      + {reason}")
            for gap in c["gaps"]:
                print(f"      - {gap}")
    elif args.cmd == "offers":
        tools = agent.Tools(connect(), args.team, live=not args.cache_only)
        print(json.dumps(tools.get_offers(args.skus), indent=2))
    elif args.cmd == "prewarm":
        prewarm(args.team)
    elif args.cmd == "ask":
        events = (trueforge_runner.run if args.trueforge else agent.run)(args.question, args.team, live=args.live)
        while events is not None:
            pending, events = _print_events(events, args.trueforge), None
            if pending and args.trueforge:
                yes = input(f"Approve cart {pending}? [y/N] ").strip().lower() == "y"
                events = trueforge_runner.decide(pending, yes, args.team)
    elif args.cmd == "approve":
        print(json.dumps(agent.approve(connect(), args.approval_id, approved=not args.reject), indent=2))


if __name__ == "__main__":
    main()
