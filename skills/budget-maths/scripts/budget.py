# Copy of server/bomscout/budget.py plus a CLI, so the sandbox needs no project install. A test keeps the two in sync.
"""Torque and power budget maths. Pure functions with JSON in and out, so the same code can run in a sandbox."""

from __future__ import annotations

G = 9.81


def _add(a, b):
    return tuple(x + y for x, y in zip(a, b))


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def joint_torques(build: dict) -> dict[str, float]:
    """Static gravity torque (N·m) on each joint in the zero pose.

    Joint origins carry translation only (no rpy), and the zero pose is the arm stretched out, the worst case for
    a pitch joint. A yaw joint carries no gravity load.
    """
    joints = build["joints"]
    links = {l["name"]: l for l in build["links"]}
    child_joints: dict[str, list[dict]] = {}
    for j in joints:
        child_joints.setdefault(j["parent"], []).append(j)

    # world position of every link frame in the zero pose
    root = next(name for name in links if all(j["child"] != name for j in joints))
    frame = {root: (0.0, 0.0, 0.0)}
    stack = [root]
    while stack:
        parent = stack.pop()
        for j in child_joints.get(parent, []):
            frame[j["child"]] = _add(frame[parent], tuple(j["origin"]))
            stack.append(j["child"])

    def subtree(link: str) -> list[str]:
        out = [link]
        for j in child_joints.get(link, []):
            out += subtree(j["child"])
        return out

    gravity = (0.0, 0.0, -G)
    torques = {}
    for j in joints:
        if j["type"] == "fixed":
            continue
        pivot = frame[j["child"]]
        tau = 0.0
        masses = [(frame[name] if name in frame else pivot, links[name]) for name in subtree(j["child"])]
        for origin, link in masses:
            com = _add(origin, tuple(link["com"]))
            r = tuple(c - p for c, p in zip(com, pivot))
            tau += _dot(tuple(j["axis"]), _cross(r, tuple(m * link["mass_kg"] for m in gravity)))
        if build.get("payload_kg") and build.get("tip_link") in subtree(j["child"]):
            tip = _add(frame[build["tip_link"]], tuple(links[build["tip_link"]]["com"]))
            r = tuple(c - p for c, p in zip(tip, pivot))
            tau += _dot(tuple(j["axis"]), _cross(r, tuple(m * build["payload_kg"] for m in gravity)))
        torques[j["name"]] = round(abs(tau), 4)
    return torques


def check_torque(build: dict, joint: str, stall_torque_nm: float, safety_factor: float = 1.5) -> dict:
    required = joint_torques(build)[joint]
    needed = required * safety_factor
    return {
        "joint": joint,
        "static_torque_nm": required,
        "safety_factor": safety_factor,
        "needed_nm": round(needed, 4),
        "stall_torque_nm": stall_torque_nm,
        "margin": round(stall_torque_nm / required, 2) if required else None,
        "ok": stall_torque_nm >= needed,
    }


def check_power(actuators: list[dict], supply: dict) -> dict:
    """actuators: [{sku, v_min, v_max, stall_current_a}], supply: {sku, v_out, max_current_a}."""
    worst = sum(a["stall_current_a"] for a in actuators)
    bad_voltage = [a["sku"] for a in actuators if not a["v_min"] <= supply["v_out"] <= a["v_max"]]
    return {
        "supply": supply["sku"],
        "v_out": supply["v_out"],
        "max_current_a": supply["max_current_a"],
        "all_stall_current_a": round(worst, 2),
        "current_ok": worst <= supply["max_current_a"],
        "voltage_ok": not bad_voltage,
        "voltage_out_of_range": bad_voltage,
    }


if __name__ == "__main__":
    import json
    import sys

    data = json.load(open(sys.argv[1]) if len(sys.argv) > 1 else sys.stdin)
    build = data["build"]
    torques = joint_torques(build)
    sku = next(a for a in data["actuators"] if a["sku"] == data["sku"])
    print(json.dumps({
        "joint_torques": torques,
        "torque": check_torque(build, data["joint"], sku["stall_torque_nm"], data["safety_factor"]),
        "power": check_power(data["actuators"], data["supply"]),
    }, indent=2))
