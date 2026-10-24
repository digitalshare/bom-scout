---
name: budget-maths
description: Re-run a robot arm's torque and power budget in the sandbox from the raw build model, to show the working behind a part recommendation.
---

# Budget maths

Use this when you recommend an actuator for a joint, or when the user asks how a torque or power number was worked out.

1. Call `get_budget_inputs(joint, sku)` on the bomscout MCP server. It returns the build model (links with mass and centre of mass, joints with origin and axis, payload), the actuators with the candidate swapped in, the power supply, and the server's own answer.
2. Write the result, minus `server_answer`, to `/tmp/budget_inputs.json` in the sandbox.
3. Run `python3 /opt/tfy/skills/budget-maths/scripts/budget.py /tmp/budget_inputs.json`.
4. Compare the output with `server_answer`. If they differ, say so and trust neither until you know why.

How the maths works (explain it in one or two lines, no more):
- Static torque on each pitch joint in the zero pose, which is the arm stretched out and the worst case: the sum over every link beyond the joint, plus the payload at the tip, of mass × g × horizontal lever arm. A yaw joint carries no gravity load.
- Needed torque = static torque × the team's safety factor. The actuator passes if its stall torque is at least that.
- Power: the sum of every actuator's stall current, compared with the supply's maximum current. All actuators stalling at once is the worst case, so treat a failure here as a warning, not a blocker. The supply voltage must sit inside each actuator's rated range.

Report: the joint, static torque, needed torque, stall torque, the margin (stall ÷ static), and the power result.
