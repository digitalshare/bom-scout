import asyncio
import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from bomscout import budget, offers
from bomscout.inputs import load_build_dir

SAMPLES = Path(__file__).resolve().parents[2] / "samples"
CATALOG = json.loads((SAMPLES / "catalog.json").read_text())


def test_urdf_and_bom_merge():
    build, team = load_build_dir(SAMPLES / "hackathon_arm")
    joints = {j.name: j for j in build.joints}
    assert joints["elbow"].sku == "xl430-w250-t"
    assert joints["elbow"].origin == (0.13, 0.0, 0.0)
    assert team["broken"][0]["joint"] == "elbow"


def test_elbow_torque_matches_hand_calc():
    build, _ = load_build_dir(SAMPLES / "hackathon_arm")
    t = budget.joint_torques(build.model_dump())
    # forearm 0.095kg@0.08m + wrist 0.03kg@0.14m + gripper 0.06kg@0.19m + payload 0.1kg@0.19m
    expected = 9.81 * (0.095 * 0.08 + 0.03 * 0.14 + 0.06 * 0.19 + 0.1 * 0.19)
    assert t["elbow"] == pytest.approx(expected, abs=1e-3)
    assert t["base_yaw"] == 0.0
    assert t["shoulder"] > t["elbow"] > t["wrist_pitch"]


def test_power_flags_all_stall_over_supply():
    servos = [{"sku": "x", "v_min": 6.5, "v_max": 12, "stall_current_a": 1.4}] * 4
    r = budget.check_power(servos, {"sku": "smps", "v_out": 12.0, "max_current_a": 5.0})
    assert r["all_stall_current_a"] == 5.6 and not r["current_ok"] and r["voltage_ok"]
    r = budget.check_power([{"sku": "xl330", "v_min": 3.7, "v_max": 6, "stall_current_a": 1.47}], {"sku": "s", "v_out": 12.0, "max_current_a": 5})
    assert r["voltage_out_of_range"] == ["xl330"]


def test_amazon_delivery_parsing():
    today = date(2026, 10, 24)
    assert offers.parse_amazon_delivery(["$12.87 delivery Friday, October 30. ", "Or fastest delivery Tuesday, October 27. "], today) == date(2026, 10, 27)
    assert offers.parse_amazon_delivery(["FREE delivery Today 4 PM - 9 PM"], today) == today
    assert offers.parse_amazon_delivery(["FREE delivery January 3"], today) == date(2027, 1, 3)


def test_delivery_estimate_skips_weekend_and_cutoff():
    vendor = CATALOG["vendors"]["robotis_us"]  # 14:00 PT cutoff, overnight shipping
    sat = datetime(2026, 10, 24, 14, 5, tzinfo=ZoneInfo("America/Los_Angeles"))
    assert offers.estimate_delivery(sat, vendor, 0) == date(2026, 10, 27)  # ships Mon, arrives Tue
    wed_am = datetime(2026, 10, 21, 9, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    assert offers.estimate_delivery(wed_am, vendor, 0) == date(2026, 10, 22)
    assert offers.estimate_delivery(wed_am, vendor, None) is None


def test_trim_drops_nav_and_keeps_listing():
    page = "Search: results for \"XL430-W250-T\"\n* [XL Series](/collections/xl)\n\nnav\nnav\n### [DYNAMIXEL XL430-W250-T](/products/x)\nRegular price $27.50\n"
    out = offers.trim_markdown(page, "XL430-W250-T")
    assert "Regular price $27.50" in out and "/collections/xl" not in out


def test_live_failure_falls_back_to_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(offers, "CACHE_DIR", tmp_path)
    now = datetime(2026, 10, 24, 14, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    cached = offers.Offer(vendor="robotis_us", sku="xl430-w250-t", title="XL430", price_usd=27.5, in_stock=True, fetched_at=now.isoformat())
    offers.write_cache("robotis_us", "xl430-w250-t", {}, [cached])

    async def boom(*a, **k):
        raise RuntimeError("blocked")

    monkeypatch.setattr(offers, "fetch_unlocker_vendor", boom)
    part = [{"sku": "xl430-w250-t", "name": "DYNAMIXEL XL430-W250-T", "query": "XL430-W250-T"}]
    [r] = asyncio.run(offers.get_offers(part, CATALOG["vendors"], "94103", "America/Los_Angeles", vendor_ids=("robotis_us",), now=now))
    assert r["source"] == "cache" and "blocked" in r["error"]
    assert r["offers"][0]["price_usd"] == 27.5 and r["offers"][0]["source"] == "cache"


def test_skill_script_matches_server_budget():
    skill = (SAMPLES.parent / "skills" / "budget-maths" / "scripts" / "budget.py").read_text()
    server = (Path(budget.__file__)).read_text()
    assert server in skill


def test_trim_keeps_product_photo_and_absolute_fixes_protocol_relative():
    page = "title\nx\ny\n* [XL Series](/collections/xl)\n![DYNAMIXEL XL430-W250-T](//robotis.us/cdn/shop/files/8.png)\n### [DYNAMIXEL XL430-W250-T](/products/x)\nRegular price $27.50\n"
    out = offers.trim_markdown(page, "XL430-W250-T")
    assert "//robotis.us/cdn/shop/files/8.png" in out and "/collections/xl" not in out
    assert offers._absolute("//robotis.us/a.png", "https://www.robotis.us/search") == "https://robotis.us/a.png"
    assert offers._absolute("/products/x", "https://www.robotis.us/search?q=1") == "https://www.robotis.us/products/x"


def test_prewarm_fetches_every_servo_adapter_and_bracket_in_batches(monkeypatch, capsys):
    from bomscout import __main__ as cli

    calls = []

    class FakeTools:
        def __init__(self, g, team, live):
            self.catalog = CATALOG

        def get_offers(self, skus):
            calls.append(skus)
            return {"results": [{"sku": s, "vendor": "robotis_us", "source": "live", "error": None,
                                 "offers": [{"price_usd": 1.0}]} for s in skus]}

    monkeypatch.setattr(cli.agent, "Tools", FakeTools)
    monkeypatch.setattr(cli, "connect", lambda: None)
    cli.prewarm("team_scout")
    wanted = [p["sku"] for p in CATALOG["parts"] if p["category"] in ("servo", "adapter", "bracket")]
    assert [s for batch in calls for s in batch] == wanted and all(len(b) <= 4 for b in calls)
    assert "live $1.00" in capsys.readouterr().out


def test_sizing_picks_cheapest_servo_that_clears_each_joint():
    from bomscout import sizing

    build, _ = load_build_dir(SAMPLES / "hackathon_arm")
    for j in build.joints:
        j.sku = None
    servos = [p for p in CATALOG["parts"] if p["category"] == "servo"]
    rows = {r["joint"]: r for r in sizing.size_actuators(build, servos, 12.0, "dxl_p2", 1.5, {})}
    # XL330 is cheaper but runs on 5 V; XL430 ($27.50) clears every joint of the desk arm
    assert {r["sku"] for r in rows.values()} == {"xl430-w250-t"}
    assert rows["shoulder"]["needed_nm"] == pytest.approx(0.8854 * 1.5, abs=1e-3)


def test_sizing_reports_joints_no_catalog_servo_can_hold():
    from bomscout import sizing

    build, _ = load_build_dir(SAMPLES / "hackathon_arm")
    for j in build.joints:
        j.sku = None
    build.payload_kg = 6.0  # about 21 N·m static at the shoulder, twice what an XM540 gives
    servos = [p for p in CATALOG["parts"] if p["category"] == "servo"]
    rows = {r["joint"]: r for r in sizing.size_actuators(build, servos, 12.0, "dxl_p2", 1.5, {})}
    assert rows["shoulder"]["sku"] is None and "no catalog part strong enough" in rows["shoulder"]["why"]
    assert rows["wrist_pitch"]["sku"] is not None
