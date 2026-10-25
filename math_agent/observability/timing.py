"""Duration is not a cumulative timestamp; parallel work is not wall time."""
from __future__ import annotations
from collections import defaultdict
import math


def union_seconds(intervals):
    total = 0.
    end = None
    for start, stop in sorted(intervals):
        if end is None or start > end:
            total += stop - start
        elif stop > end:
            total += stop - end
        end = stop if end is None else max(end, stop)
    return round(total, 6)


def summarize(report):
    stages = defaultdict(lambda: {"calls": 0, "work_seconds_sum": 0., "missing_duration": 0,
        "input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "cached_input_tokens": 0, "missing_usage": 0})
    intervals, started, completed = [], set(), set()
    reuse = executed = 0
    for entry in report.get("audit", []):
        if entry.get("kind") == "model_request_started":
            started.add(entry.get("request_id"))
        if entry.get("kind") == "v2_receipt":
            reuse += bool(entry.get("reused_from"))
            executed += not bool(entry.get("reused_from"))
        if entry.get("kind") != "model" or entry.get("not_sent"):
            continue
        completed.add(entry.get("request_id"))
        stage = stages[entry.get("stage", "unknown")]
        stage["calls"] += 1
        duration = entry.get("duration_seconds")
        if isinstance(duration, (float, int)) and math.isfinite(duration) and duration >= 0:
            stage["work_seconds_sum"] += duration
            end = entry.get("seconds")
            if isinstance(end, (float, int)) and math.isfinite(end) and end >= duration:
                intervals.append((end - duration, end))
        else:
            stage["missing_duration"] += 1
        usage = entry.get("usage") or {}
        stage["missing_usage"] += not bool(usage)
        stage["input_tokens"] += usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0
        stage["output_tokens"] += usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0
        stage["reasoning_tokens"] += (usage.get("output_tokens_details") or usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0) or 0
        stage["cached_input_tokens"] += usage.get("prompt_cache_hit_tokens", (usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)) or 0
    for value in stages.values():
        value["work_seconds_sum"] = round(value["work_seconds_sum"], 6)
    return {"status": report.get("status"), "total_seconds": report.get("seconds"), "model_stages": dict(stages),
        "model_work_seconds_sum": round(sum(v["work_seconds_sum"] for v in stages.values()), 6),
        "model_interval_union_seconds": union_seconds(intervals),
        "unknown_completion_requests": sorted(s for s in started - completed if s),
        "receipt_observations": executed, "receipt_reuses": reuse,
        "note": "reasoning_tokens are a subset of output_tokens; no cost is fabricated for missing usage. Missing durations are not replaced by cumulative timestamps."}
