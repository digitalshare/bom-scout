"""Shared data shapes. Every input format (BOM, URDF, free text) ends up as a Build."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Link(BaseModel):
    name: str
    mass_kg: float = 0.0
    com: tuple[float, float, float] = (0.0, 0.0, 0.0)  # in the link frame


class Joint(BaseModel):
    name: str
    type: str = "revolute"
    parent: str
    child: str
    origin: tuple[float, float, float] = (0.0, 0.0, 0.0)
    axis: tuple[float, float, float] = (0.0, 0.0, 1.0)
    effort_nm: float | None = None
    sku: str | None = None  # the actuator driving this joint, from the BOM


class BomLine(BaseModel):
    sku: str
    qty: int = 1
    joint: str | None = None
    note: str = ""


class Build(BaseModel):
    id: str
    links: list[Link] = Field(default_factory=list)
    joints: list[Joint] = Field(default_factory=list)
    bom: list[BomLine] = Field(default_factory=list)
    payload_kg: float = 0.0
    tip_link: str | None = None


class Substitute(BaseModel):
    sku: str
    name: str
    score: float
    ok: bool  # passes every hard rule (protocol, voltage, torque)
    reasons: list[str]  # one line per hop that matched, in path order
    gaps: list[str]  # what the team still needs to buy or change
    path: list[str]  # node labels for drawing: broken part -> shared specs -> substitute
    owned_qty: int = 0


class Offer(BaseModel):
    vendor: str
    sku: str
    title: str
    price_usd: float | None
    in_stock: bool | None
    stock_qty: int | None = None
    lead_time: str | None = None  # vendor's own wording, e.g. "Estimated: 2 weeks"
    ship_days: int | None = None  # days before it leaves the warehouse
    deliver_by: str | None = None  # ISO date of the earliest arrival at the venue
    arrives_today: bool = False
    url: str | None = None
    fetched_at: str
    source: Literal["live", "cache"] = "live"
