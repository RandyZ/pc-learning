"""Optional metadata-only LangSmith sink; never serialize task state or exceptions.

Automatic LangGraph tracing stays disabled. This sink explicitly publishes span
hierarchy and numeric usage. A broken observability backend never retries work.
"""
from __future__ import annotations
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from functools import wraps
import os
import uuid

NAMES = frozenset({"job", "source", "source_review", "solve", "observe", "review", "repair",
    "math_repair", "diagram", "review_plan", "adjudicate", "reference_math", "math_review",
    "draw", "draw_patch", "graph_review", "diagram_review", "deferred_review", "model", "tool"})
USAGE = frozenset({"input_tokens", "output_tokens", "prompt_tokens", "completion_tokens",
    "total_tokens", "reasoning_tokens", "cached_tokens", "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens", "input_tokens_details", "output_tokens_details",
    "prompt_tokens_details", "completion_tokens_details"})


def usage_only(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, item in value.items():
        if key not in USAGE:
            continue
        if isinstance(item, dict):
            result[key] = usage_only(item)
        elif type(item) in (int, float) and item >= 0:
            result[key] = item
    return result


class TraceSink:
    def __init__(self, client=None, project="pc-learning-classic"):
        self.client, self.project = client, project
        self._parent = ContextVar("classic_trace_parent", default=None)
        self.failures = 0

    @classmethod
    def from_env(cls):
        if os.getenv("LANGSMITH_TRACING", "false").lower() != "true" or not os.getenv("LANGSMITH_API_KEY"):
            return cls()
        try:
            from langsmith import Client
            client = Client(hide_inputs=True, hide_outputs=True,
                            auto_batch_tracing=False, timeout_ms=2000)
            return cls(client, os.getenv("LANGSMITH_PROJECT", "pc-learning-classic"))
        except Exception:
            return cls()

    def _send(self, operation, *args, **kwargs):
        try:
            getattr(self.client, operation)(*args, **kwargs)
        except Exception:
            # Do not log exception text: HTTP errors may contain credentials/content.
            self.failures += 1

    def _start(self, name, when, *, run_type="chain", metadata=None):
        run_id = uuid.uuid4()
        parent = self._parent.get()
        trace_id = parent[1] if parent else run_id
        segment = when.strftime("%Y%m%dT%H%M%S%fZ") + str(run_id)
        order = parent[2] + "." + segment if parent else segment
        self._send("create_run", name=name if name in NAMES else "job", inputs={},
            run_type=run_type, id=run_id, trace_id=trace_id, dotted_order=order,
            parent_run_id=parent[0] if parent else None, start_time=when,
            project_name=self.project, extra={"metadata": metadata or {}})
        return run_id, trace_id, order

    @contextmanager
    def span(self, name):
        if self.client is None:
            yield
            return
        ref = self._start(name, datetime.now(timezone.utc))
        token = self._parent.set(ref)
        failed = False
        try:
            yield
        except BaseException:
            failed = True
            raise
        finally:
            self._parent.reset(token)
            self._send("update_run", ref[0], end_time=datetime.now(timezone.utc),
                outputs={}, error="operation_failed" if failed else None,
                trace_id=ref[1], dotted_order=ref[2])

    def completed(self, kind, detail):
        if self.client is None or kind != "model":
            return
        elapsed = detail.get("seconds")
        valid = type(elapsed) in (int, float) and 0 <= elapsed <= 86400
        end = datetime.now(timezone.utc)
        metadata = {"duration_available": valid, "usage": usage_only(detail.get("usage"))}
        stage = detail.get("stage")
        if stage in NAMES:
            metadata["stage"] = stage
        for key in ("call", "thinking", "not_sent", "status"):
            if type(detail.get(key)) in (bool, int):
                metadata[key] = detail[key]
        ref = self._start("model", end - timedelta(seconds=elapsed if valid else 0),
                          run_type="llm", metadata=metadata)
        self._send("update_run", ref[0], end_time=end, outputs={},
            error="model_call_failed" if detail.get("error") else None,
            trace_id=ref[1], dotted_order=ref[2])


def traced_node(function):
    @wraps(function)
    def wrapped(state, runtime):
        sink = getattr(runtime.context.job, "trace_sink", None)
        with sink.span(function.__name__) if sink else nullcontext():
            return function(state, runtime)
    return wrapped
