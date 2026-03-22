#!/usr/bin/env python3
"""Validate a build123d assembly for interference, clearance, and swept-volume collisions.

The script expects a `--script` argument containing build123d Python that defines:
  - `parts`: dict[str, Part] — named solids in the assembly
  - `sweeps` (optional): list[dict] — moving parts with sweep definitions

Example script:

    from build123d import *

    with BuildPart() as enclosure:
        Box(100, 80, 50)
        offset(amount=-3, openings=enclosure.faces().sort_by(Axis.Z)[-1:])

    with BuildPart() as pcb:
        with Locations((0, 0, 5)):
            Box(60, 40, 2)

    with BuildPart() as servo_arm:
        with Locations((30, 0, 25)):
            Box(5, 20, 3)

    parts = {
        "enclosure": enclosure.part,
        "pcb": pcb.part,
        "servo_arm": servo_arm.part,
    }

    # Optional: define swept volumes for moving parts
    # Each sweep rotates a part around an axis through an angular range
    sweeps = [
        {
            "name": "servo_arm",
            "axis_origin": (30, 0, 25),   # pivot point (mm)
            "axis_direction": (0, 1, 0),  # rotation axis
            "angle_start": -45,            # degrees
            "angle_end": 45,
            "angle_step": 5,               # check every 5°
        },
    ]

Modes:
  --mode static     Check all part pairs for interference (Boolean intersection volume > 0)
  --mode clearance  Check minimum clearance between all part pairs
  --mode sweep      Generate swept volumes for moving parts, check against static parts
  --mode full       Run all three checks (default)
"""

import argparse
import itertools
import json
import math
import sys
import traceback
from typing import Any

from helpers import exec_script, get_part, output_json, output_error, WORKSPACE


def check_static_interference(parts: dict) -> list[dict]:
    """Check all part pairs for Boolean intersection. Returns list of interferences."""
    interferences = []
    for (name_a, part_a), (name_b, part_b) in itertools.combinations(parts.items(), 2):
        try:
            intersection = part_a & part_b
            vol = intersection.volume if hasattr(intersection, "volume") else 0.0
            if vol > 0.01:  # tolerance: 0.01 mm³
                bb = intersection.bounding_box()
                interferences.append({
                    "part_a": name_a,
                    "part_b": name_b,
                    "overlap_volume_mm3": round(vol, 3),
                    "overlap_bbox_mm": {
                        "x": round(bb.size.X, 2),
                        "y": round(bb.size.Y, 2),
                        "z": round(bb.size.Z, 2),
                    },
                    "overlap_center_mm": {
                        "x": round(bb.center().X, 2),
                        "y": round(bb.center().Y, 2),
                        "z": round(bb.center().Z, 2),
                    },
                })
        except Exception:
            interferences.append({
                "part_a": name_a,
                "part_b": name_b,
                "error": f"Boolean intersection failed: {traceback.format_exc().splitlines()[-1]}",
            })
    return interferences


def check_clearance(parts: dict) -> list[dict]:
    """Approximate minimum clearance between all part pairs using bounding box gap."""
    # True minimum distance requires OCC's BRepExtrema_DistShapeShape which is expensive.
    # We use bounding box gap as a fast conservative estimate (actual clearance >= bbox gap).
    clearances = []
    for (name_a, part_a), (name_b, part_b) in itertools.combinations(parts.items(), 2):
        try:
            bb_a = part_a.bounding_box()
            bb_b = part_b.bounding_box()

            # Gap per axis: distance between nearest faces of the two bounding boxes
            def axis_gap(a_min, a_max, b_min, b_max):
                if a_max < b_min:
                    return b_min - a_max
                if b_max < a_min:
                    return a_min - b_max
                return 0.0  # overlapping on this axis

            gx = axis_gap(bb_a.min.X, bb_a.max.X, bb_b.min.X, bb_b.max.X)
            gy = axis_gap(bb_a.min.Y, bb_a.max.Y, bb_b.min.Y, bb_b.max.Y)
            gz = axis_gap(bb_a.min.Z, bb_a.max.Z, bb_b.min.Z, bb_b.max.Z)

            # If all axes overlap, parts may intersect — clearance is 0 (or negative)
            if gx == 0 and gy == 0 and gz == 0:
                min_clearance = 0.0
            else:
                # Euclidean distance between nearest bbox corners
                min_clearance = math.sqrt(gx**2 + gy**2 + gz**2)

            clearances.append({
                "part_a": name_a,
                "part_b": name_b,
                "min_clearance_mm": round(min_clearance, 3),
                "method": "bounding_box_gap",
                "note": "Conservative estimate. Actual clearance >= this value." if min_clearance > 0 else "Bounding boxes overlap. Run static interference check.",
            })
        except Exception:
            clearances.append({
                "part_a": name_a,
                "part_b": name_b,
                "error": traceback.format_exc().splitlines()[-1],
            })
    return clearances


def check_swept_volumes(parts: dict, sweeps: list[dict]) -> list[dict]:
    """For each sweep definition, rotate the part through the angular range and check
    each pose against all static parts for interference."""
    from build123d import Axis, Vector, Rot, copy as bd_copy

    collisions = []
    for sweep in sweeps:
        name = sweep["name"]
        if name not in parts:
            collisions.append({"sweep": name, "error": f"Part '{name}' not found in parts dict."})
            continue

        moving_part = parts[name]
        origin = Vector(*sweep["axis_origin"])
        direction = Vector(*sweep["axis_direction"])
        angle_start = sweep.get("angle_start", -45)
        angle_end = sweep.get("angle_end", 45)
        angle_step = sweep.get("angle_step", 5)

        static_parts = {k: v for k, v in parts.items() if k != name}

        # Build the swept volume as a union of all rotated poses
        swept = None
        angles_checked = []

        angle = angle_start
        while angle <= angle_end:
            try:
                # Rotate a copy of the part around the defined axis
                rotated = moving_part.rotate(Axis(origin, direction), angle)

                if swept is None:
                    swept = rotated
                else:
                    swept = swept + rotated  # union

                angles_checked.append(angle)
            except Exception:
                collisions.append({
                    "sweep": name,
                    "angle_deg": angle,
                    "error": f"Rotation failed: {traceback.format_exc().splitlines()[-1]}",
                })
            angle += angle_step

        if swept is None:
            collisions.append({"sweep": name, "error": "No valid rotated poses generated."})
            continue

        # Export swept volume for visualization
        try:
            from build123d import export_step
            swept_path = WORKSPACE / f"swept_{name}.step"
            export_step(swept, str(swept_path))
        except Exception:
            pass  # non-critical

        # Check swept volume against each static part
        for static_name, static_part in static_parts.items():
            try:
                intersection = swept & static_part
                vol = intersection.volume if hasattr(intersection, "volume") else 0.0
                if vol > 0.01:
                    bb = intersection.bounding_box()
                    collisions.append({
                        "sweep": name,
                        "collides_with": static_name,
                        "overlap_volume_mm3": round(vol, 3),
                        "overlap_center_mm": {
                            "x": round(bb.center().X, 2),
                            "y": round(bb.center().Y, 2),
                            "z": round(bb.center().Z, 2),
                        },
                        "angle_range_deg": [angle_start, angle_end],
                        "angles_checked": len(angles_checked),
                        "swept_volume_path": str(swept_path) if (WORKSPACE / f"swept_{name}.step").exists() else None,
                    })
            except Exception:
                collisions.append({
                    "sweep": name,
                    "collides_with": static_name,
                    "error": traceback.format_exc().splitlines()[-1],
                })

        if not any(c.get("collides_with") for c in collisions if c.get("sweep") == name):
            collisions.append({
                "sweep": name,
                "collides_with": None,
                "status": "clear",
                "angle_range_deg": [angle_start, angle_end],
                "angles_checked": len(angles_checked),
            })

    return collisions


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--script", required=True)
    parser.add_argument("--mode", choices=["static", "clearance", "sweep", "full"], default="full")
    parser.add_argument("--min-clearance", type=float, default=1.0, help="Minimum acceptable clearance in mm")
    args = parser.parse_args()

    r = exec_script(args.script)
    if not r["ok"]:
        output_error(r["error"], r["stdout"])

    parts_raw = r.get("parts")
    if not parts_raw or not isinstance(parts_raw, dict):
        output_error("Script must define `parts = {'name': part, ...}` dict.")

    # Extract Part objects from BuildPart contexts
    parts = {}
    for name, ctx in parts_raw.items():
        parts[name] = get_part(ctx) if hasattr(ctx, "part") else ctx

    # Sweeps are in the exec namespace — re-extract from the in-process exec
    # (exec_script already validated the script, so the in-process re-exec is safe)
    ns = {}
    exec(args.script, ns)
    sweeps = ns.get("sweeps", [])

    result: dict[str, Any] = {
        "success": True,
        "part_count": len(parts),
        "parts": list(parts.keys()),
    }

    # Static interference
    if args.mode in ("static", "full"):
        interferences = check_static_interference(parts)
        result["static_interference"] = {
            "checked_pairs": len(list(itertools.combinations(parts, 2))),
            "interferences_found": len([i for i in interferences if "overlap_volume_mm3" in i]),
            "details": interferences,
        }

    # Clearance
    if args.mode in ("clearance", "full"):
        clearances = check_clearance(parts)
        violations = [c for c in clearances if c.get("min_clearance_mm", 999) < args.min_clearance and "error" not in c]
        result["clearance"] = {
            "min_acceptable_mm": args.min_clearance,
            "violations": len(violations),
            "details": clearances,
        }

    # Swept volume
    if args.mode in ("sweep", "full") and sweeps:
        collisions = check_swept_volumes(parts, sweeps)
        result["swept_volume"] = {
            "sweeps_defined": len(sweeps),
            "collisions_found": len([c for c in collisions if c.get("overlap_volume_mm3", 0) > 0]),
            "details": collisions,
        }

    # Verdict
    has_interference = any(
        i.get("overlap_volume_mm3", 0) > 0
        for i in result.get("static_interference", {}).get("details", [])
    )
    has_clearance_violation = result.get("clearance", {}).get("violations", 0) > 0
    has_sweep_collision = any(
        c.get("overlap_volume_mm3", 0) > 0
        for c in result.get("swept_volume", {}).get("details", [])
    )

    if has_interference or has_sweep_collision:
        result["verdict"] = "FAIL"
        result["verdict_reason"] = []
        if has_interference:
            result["verdict_reason"].append("Static interference detected between parts.")
        if has_sweep_collision:
            result["verdict_reason"].append("Moving parts collide during sweep.")
        if has_clearance_violation:
            result["verdict_reason"].append(f"Clearance below {args.min_clearance}mm threshold.")
    elif has_clearance_violation:
        result["verdict"] = "WARN"
        result["verdict_reason"] = [f"Clearance below {args.min_clearance}mm threshold."]
    else:
        result["verdict"] = "PASS"
        result["verdict_reason"] = ["No interference, clearance OK."]

    result["stdout"] = r["stdout"]
    output_json(result)


if __name__ == "__main__":
    main()
