"""Reads a build from a URDF, a BOM CSV, or a one-line description."""

from __future__ import annotations

import csv
import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path

from .models import BomLine, Build, Joint, Link

DEFAULT_MODEL = os.environ.get("BOMSCOUT_MODEL", "gpt-5.5")


def _vec(text: str | None, default=(0.0, 0.0, 0.0)) -> tuple[float, float, float]:
    if not text:
        return default
    x, y, z = (float(v) for v in text.split())
    return (x, y, z)


def parse_urdf(path: Path) -> Build:
    root = ET.parse(path).getroot()
    links = []
    for el in root.findall("link"):
        inertial = el.find("inertial")
        mass = com = None
        if inertial is not None:
            mass_el = inertial.find("mass")
            mass = float(mass_el.get("value")) if mass_el is not None else 0.0
            origin = inertial.find("origin")
            com = _vec(origin.get("xyz") if origin is not None else None)
        links.append(Link(name=el.get("name"), mass_kg=mass or 0.0, com=com or (0.0, 0.0, 0.0)))
    joints = []
    for el in root.findall("joint"):
        origin, axis, limit = el.find("origin"), el.find("axis"), el.find("limit")
        joints.append(
            Joint(
                name=el.get("name"),
                type=el.get("type"),
                parent=el.find("parent").get("link"),
                child=el.find("child").get("link"),
                origin=_vec(origin.get("xyz") if origin is not None else None),
                axis=_vec(axis.get("xyz") if axis is not None else None, (1.0, 0.0, 0.0)),
                effort_nm=float(limit.get("effort")) if limit is not None and limit.get("effort") else None,
            )
        )
    return Build(id=root.get("name"), links=links, joints=joints)


def parse_bom(path: Path) -> list[BomLine]:
    with path.open() as f:
        return [
            BomLine(sku=row["sku"].strip().lower(), qty=int(row.get("qty") or 1), joint=row.get("joint") or None, note=row.get("note") or "")
            for row in csv.DictReader(f)
        ]


def load_build_dir(build_dir: Path) -> tuple[Build, dict]:
    """A build folder holds arm.urdf (optional), bom.csv and team.json."""
    team = json.loads((build_dir / "team.json").read_text())
    urdfs = list(build_dir.glob("*.urdf"))
    build = parse_urdf(urdfs[0]) if urdfs else Build(id=team["build"])
    build.bom = parse_bom(build_dir / "bom.csv")
    by_joint = {line.joint: line.sku for line in build.bom if line.joint}
    for j in build.joints:
        j.sku = by_joint.get(j.name)
    build.payload_kg = team.get("payload_kg", 0.0)
    build.tip_link = team.get("tip_link")
    return build, team


DESCRIBE_INSTRUCTIONS = """You turn a one-line robot description into a rough serial-arm model for a torque budget.
Lay the arm out stretched along +x at the zero pose. Joint axes: base yaw is (0,0,1); pitch joints are (0,1,0).
Each link mass includes the actuator at its far end. Use realistic hobby/competition masses and lengths.
Pick actuator skus only from this catalog list, or leave sku null: {skus}"""


def describe_to_build(text: str, catalog_skus: list[str]) -> Build:
    from openai import OpenAI

    resp = OpenAI().responses.parse(
        model=DEFAULT_MODEL,
        instructions=DESCRIBE_INSTRUCTIONS.format(skus=", ".join(catalog_skus)),
        input=text,
        text_format=Build,
    )
    return resp.output_parsed
