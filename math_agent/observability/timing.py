"""Separate cumulative audit timestamps, summed work and overlapping wall time."""
from collections import defaultdict
import math


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _union(intervals):
    total = 0.0
    end = None
    for left, right in sorted(intervals):
        if end is None or left > end:
            total += right - left
        elif right > end:
            total += right - end
        end = right if end is None else max(end, right)
    return round(total, 6)


def summarize(report):
    stages = defaultdict(lambda: {"calls": 0, "not_sent": 0, "model_seconds": 0.0,
        "missing_duration": 0, "completion_tokens": 0, "missing_usage": 0})
    intervals = []
    ocr = []
    for entry in report.get("audit", []):
        if entry.get("kind") == "model":
            row = stages[entry["stage"]]
            if entry.get("not_sent"):
                row["not_sent"] += 1
                continue
            row["calls"] += 1
            duration, end = entry.get("duration_seconds"), entry.get("seconds")
            if _number(duration):
                row["model_seconds"] += duration
                if _number(end):
                    intervals.append((max(0.0, end - duration), end))
            else:
                row["missing_duration"] += 1
            usage = entry.get("usage") or {}
            tokens = usage.get("output_tokens", usage.get("completion_tokens"))
            if _number(tokens):
                row["completion_tokens"] += tokens
            else:
                row["missing_usage"] += 1
        if entry.get("kind") == "ocr" and entry.get("status") == "done":
            ocr.append({k: entry.get(k) for k in ("image_id", "elapsed_seconds", "cache_hit")})
    for row in stages.values():
        row["model_seconds"] = round(row["model_seconds"], 6)
    return {"status": report.get("status"), "total_seconds": report.get("seconds"),
        "model_stages": dict(stages), "model_wall_seconds_known": _union(intervals), "ocr": ocr,
        "note": "duration_seconds is request duration; seconds is cumulative. Missing timing/usage stays unknown. Reasoning is not added twice."}
