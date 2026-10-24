"""Live price, stock and delivery date per vendor via Bright Data, cached on disk so the demo survives a bad network.

Amazon comes from Bright Data's Amazon dataset, which takes the venue zip code and returns delivery dates.
The other vendors come through the Web Unlocker as markdown, which OpenAI turns into offers.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel

from .models import Offer

API = "https://api.brightdata.com"
AMAZON_DATASET = "gd_l7q7dkf244hwjntr0"
CACHE_DIR = Path(os.environ.get("BOMSCOUT_CACHE", Path(__file__).resolve().parents[2] / "cache" / "offers"))
DEFAULT_MODEL = os.environ.get("BOMSCOUT_MODEL", "gpt-5.5")
# Mouser answers the Web Unlocker with an empty page, and RobotShop needs Bright Data KYC; both stay off by default.
DEFAULT_VENDORS = ("robotis_us", "digikey", "amazon")


def _headers() -> dict:
    return {"Authorization": f"Bearer {os.environ['BRIGHTDATA_API_TOKEN']}"}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


# ---------- dates ----------

def add_business_days(start: date, days: int) -> date:
    d = start
    while days > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days -= 1
    return d


def estimate_delivery(now: datetime, vendor: dict, ship_days: int | None) -> date | None:
    """Order now, wait ship_days for it to leave the warehouse, then the vendor's fastest shipping option."""
    if ship_days is None:
        return None
    local = now.astimezone(ZoneInfo(vendor["tz"]))
    cutoff_h, cutoff_m = (int(x) for x in vendor["cutoff_local"].split(":"))
    ships = local.date()
    if local.weekday() >= 5 or (local.hour, local.minute) >= (cutoff_h, cutoff_m):
        ships = add_business_days(ships, 1)
    ships = add_business_days(ships, ship_days) if ship_days else ships
    return add_business_days(ships, vendor["fastest_ship_days"]) if vendor["fastest_ship_days"] else ships


MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"], 1)}


def parse_amazon_delivery(lines: list[str], today: date) -> date | None:
    """Earliest date in strings like 'Or fastest delivery Tuesday, October 27.' or 'FREE delivery Today 4 PM - 9 PM'."""
    best = None
    for line in lines:
        low = line.lower()
        if "today" in low or "tonight" in low:
            return today
        if "tomorrow" in low:
            d = today + timedelta(days=1)
        else:
            m = re.search(r"(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2})", low)
            if not m:
                continue
            d = date(today.year, MONTHS[m.group(1)], int(m.group(2)))
            if d < today:
                d = date(today.year + 1, d.month, d.day)
        best = d if best is None or d < best else best
    return best


# ---------- vendors ----------

async def unlocker_markdown(client: httpx.AsyncClient, url: str) -> str:
    resp = await client.post(
        f"{API}/request",
        headers=_headers(),
        json={"zone": os.environ.get("BRIGHTDATA_ZONE", "mcp_unlocker"), "url": url, "format": "raw", "data_format": "markdown"},
        timeout=90,
    )
    resp.raise_for_status()
    if not resp.text.strip():
        raise RuntimeError(f"empty page from {url}")
    return resp.text


NAV_LINE = re.compile(r"^\s*(?:[*-]\s+)?!?\[.*\]\([^)]*\)\s*$")


def trim_markdown(text: str, query: str, before: int = 15, after: int = 130, cap: int = 15_000) -> str:
    """Keep only the lines near mentions of the part, dropping blank lines and bare navigation links."""
    lines = [l for l in text.splitlines() if l.strip() and not NAV_LINE.match(l)]
    key = _norm(query)
    keep: set[int] = set()
    for i, line in enumerate(lines):
        if i > 2 and key and key in _norm(line):  # the first lines are the page title, which repeats the query
            keep.update(range(max(0, i - before), min(len(lines), i + after)))
    return "\n".join(lines[i] for i in sorted(keep))[:cap]


class _Listing(BaseModel):
    title: str
    exact_match: bool  # the listing is the requested model itself, not an accessory, bundle or other model
    price_usd: float | None
    in_stock: bool | None
    stock_qty: int | None
    lead_time: str | None
    ship_days: int | None
    url: str | None


class _Listings(BaseModel):
    listings: list[_Listing]


PARSE_INSTRUCTIONS = """You read a vendor search or product page (markdown) and list the listings for ONE requested part.
- exact_match is true when the listing is the requested model itself. Vendors often title a listing by a manufacturer
  number (e.g. "902-0155-000") and name the model in the description; that still counts. Bundles, multi-packs,
  accessories and other models (e.g. 2XL430 when XL430 was asked for) do not.
- price_usd: unit price for quantity 1.
- lead_time: copy the vendor's own wording ("Will ship in approximately 7 days", "Lead Time Required Estimated: 2 weeks", "In stock").
- ship_days: business days before it leaves the warehouse. "2 weeks" = 10, "1-2 months" = 30.
  On a store's own search results, a listing with no lead-time, backorder or sold-out note is in stock: in_stock = true,
  ship_days = 0, lead_time = "no lead-time note on listing". Otherwise unknown = null.
- url: absolute URL of the listing if the page has one.
Never invent a listing or a number that is not on the page."""


def parse_listings(markdown: str, vendor_name: str, part_query: str, base_url: str) -> list[_Listing]:
    from openai import OpenAI

    resp = OpenAI().responses.parse(
        model=DEFAULT_MODEL,
        instructions=PARSE_INSTRUCTIONS,
        input=f"VENDOR: {vendor_name} ({base_url})\nREQUESTED PART: {part_query}\n\nPAGE:\n{markdown}",
        text_format=_Listings,
    )
    return resp.output_parsed.listings


async def fetch_unlocker_vendor(client, vendor_id: str, vendor: dict, sku: str, query: str, now: datetime) -> tuple[dict, list[Offer]]:
    url = vendor["search_url"].format(q=quote_plus(query))
    text = await unlocker_markdown(client, url)
    trimmed = trim_markdown(text, query)
    listings = await asyncio.to_thread(parse_listings, trimmed, vendor["name"], query, url) if trimmed else []
    fetched = now.isoformat()
    offers = []
    for l in listings:
        if not l.exact_match:
            continue
        eta = estimate_delivery(now, vendor, l.ship_days)
        offers.append(Offer(
            vendor=vendor_id, sku=sku, title=l.title, price_usd=l.price_usd, in_stock=l.in_stock, stock_qty=l.stock_qty,
            lead_time=l.lead_time, ship_days=l.ship_days, deliver_by=eta.isoformat() if eta else None,
            arrives_today=eta == now.date() if eta else False, url=_absolute(l.url, url), fetched_at=fetched,
        ))
    return {"url": url, "page": trimmed}, offers


def _absolute(link: str | None, base: str) -> str | None:
    if not link or link.startswith("http"):
        return link
    root = re.match(r"https?://[^/]+", base).group(0)
    return root + ("" if link.startswith("/") else "/") + link


async def amazon_records(client: httpx.AsyncClient, keyword: str, zipcode: str, timeout_s: int = 150) -> list[dict]:
    params = {"dataset_id": AMAZON_DATASET, "type": "discover_new", "discover_by": "keyword", "limit_per_input": 8, "format": "json"}
    resp = await client.post(f"{API}/datasets/v3/scrape", params=params, headers=_headers(),
                             json={"input": [{"keyword": keyword, "zipcode": zipcode}]}, timeout=timeout_s)
    resp.raise_for_status()
    body = resp.json()
    if isinstance(body, list):
        return body
    snapshot = body.get("snapshot_id")
    if not snapshot:
        return body.get("data", [])
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(5)
        prog = (await client.get(f"{API}/datasets/v3/progress/{snapshot}", headers=_headers())).json()
        if prog.get("status") == "ready":
            return (await client.get(f"{API}/datasets/v3/snapshot/{snapshot}", params={"format": "json"}, headers=_headers(), timeout=60)).json()
        if prog.get("status") == "failed":
            raise RuntimeError(f"amazon snapshot {snapshot} failed")
    raise TimeoutError(f"amazon snapshot {snapshot} not ready after {timeout_s}s")


async def fetch_amazon(client, sku: str, keyword: str, query: str, zipcode: str, now: datetime) -> tuple[dict, list[Offer]]:
    records = await amazon_records(client, keyword, zipcode)
    key = _norm(query)  # model number, e.g. "xc430w150t"
    offers = []
    for r in records:
        if r.get("error") or key not in _norm((r.get("title") or "") + " " + (r.get("model_number") or "") + json.dumps(r.get("product_details") or [])):
            continue
        eta = parse_amazon_delivery(r.get("delivery") or [], now.date())
        offers.append(Offer(
            vendor="amazon", sku=sku, title=r.get("title") or "", price_usd=r.get("final_price"),
            in_stock=r.get("is_available"), stock_qty=r.get("max_quantity_available"), lead_time=r.get("availability"),
            deliver_by=eta.isoformat() if eta else None, arrives_today=eta == now.date() if eta else False,
            url=r.get("url"), fetched_at=now.isoformat(),
        ))
    slim = [{k: r.get(k) for k in ("title", "asin", "final_price", "availability", "delivery", "url", "zipcode")} for r in records]
    return {"zipcode": zipcode, "records": slim}, offers


# ---------- cache + fan-out ----------

def _cache_path(vendor: str, sku: str) -> Path:
    return CACHE_DIR / vendor / f"{sku}.json"


def read_cache(vendor: str, sku: str) -> list[Offer] | None:
    path = _cache_path(vendor, sku)
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return [Offer(**{**o, "source": "cache"}) for o in data["offers"]]


def write_cache(vendor: str, sku: str, raw: dict, offers: list[Offer]) -> None:
    path = _cache_path(vendor, sku)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"raw": raw, "offers": [o.model_dump() for o in offers]}, indent=2))


async def _one(client, vendor_id: str, vendor: dict, sku: str, name: str, query: str, zipcode: str, now: datetime, live: bool) -> dict:
    if not live:
        cached = read_cache(vendor_id, sku)
        return {"vendor": vendor_id, "offers": cached or [], "source": "cache", "error": None if cached is not None else "no cache"}
    try:
        if vendor_id == "amazon":
            raw, offers = await fetch_amazon(client, sku, name, query, zipcode, now)
        else:
            raw, offers = await fetch_unlocker_vendor(client, vendor_id, vendor, sku, query, now)
        write_cache(vendor_id, sku, raw, offers)
        return {"vendor": vendor_id, "offers": offers, "source": "live", "error": None}
    except Exception as e:  # fall back to the last good scrape
        cached = read_cache(vendor_id, sku)
        return {"vendor": vendor_id, "offers": cached or [], "source": "cache", "error": f"{type(e).__name__}: {e}"[:300]}


async def get_offers(parts: list[dict], vendors: dict, zipcode: str, tz: str, live: bool = True,
                     vendor_ids: tuple[str, ...] = DEFAULT_VENDORS, now: datetime | None = None) -> list[dict]:
    """parts: [{sku, name, query}]. Returns one entry per (part, vendor) with offers and whether they are live or cached."""
    now = now or datetime.now(ZoneInfo(tz))
    async with httpx.AsyncClient() as client:
        jobs = [
            _one(client, vid, vendors[vid], p["sku"], p.get("name", p["query"]), p["query"], zipcode, now, live)
            for p in parts for vid in vendor_ids
        ]
        results = await asyncio.gather(*jobs)
    out = []
    for (p, vid), r in zip([(p, vid) for p in parts for vid in vendor_ids], results):
        out.append({"sku": p["sku"], **r, "offers": [o.model_dump() for o in r["offers"]]})
    return out
