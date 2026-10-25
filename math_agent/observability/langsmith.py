"""Optional, metadata-only trace export. Never send prompts, images or exception text."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import os
import re

_CURRENT = ContextVar("pc_learning_safe_span", default=None)


class SafeTrace:
    def __init__(self, job_id, profile):
        self.client = self.root = None
        if os.getenv("LANGSMITH_TRACING", "false").lower() != "true" or not os.getenv("LANGSMITH_API_KEY"):
            return
        try:
            from langsmith import Client, RunTree
            self.client = Client(hide_inputs=True, hide_outputs=True)
            self.root = RunTree(name="evidence_v2", run_type="chain", inputs={}, client=self.client,
                project_name=os.getenv("LANGSMITH_PROJECT", "pc-learning-v2"),
                extra={"metadata": {"job_id": job_id, "profile": profile}})
            self.root.post()
        except Exception:
            self.client = self.root = None

    @contextmanager
    def span(self, name):
        child = None
        token = None
        if self.root is not None:
            try:
                parent = _CURRENT.get() or self.root
                child = parent.create_child(name=name, run_type="chain", inputs={})
                child.post()
                token = _CURRENT.set(child)
            except Exception:
                child = None
        error = None
        try:
            yield
        except BaseException as exc:
            error = type(exc).__name__  # Exception messages can contain images/secrets.
            raise
        finally:
            if token is not None:
                _CURRENT.reset(token)
            if child is not None:
                try:
                    child.end(outputs={}, error=error)
                    child.patch()
                except Exception:
                    pass

    def completed(self, kind, detail):
        if self.root is None or kind not in {"model", "v2_math_batch"}:
            return
        try:
            duration = max(0., float(detail.get("seconds", 0)))
            end = datetime.now(timezone.utc)
            stage = detail.get("stage", "unknown")
            if not re.fullmatch(r"[a-z_]{1,40}", stage):
                stage = "unknown"
            meta = {"duration_seconds": duration}
            if type(detail.get("call")) is int:
                meta["call"] = detail["call"]
            usage = detail.get("usage") or {}
            for field in ("prompt_tokens", "completion_tokens", "input_tokens", "output_tokens", "prompt_cache_hit_tokens"):
                value = usage.get(field)
                if type(value) is int and value >= 0:
                    meta[field] = value
            run = (_CURRENT.get() or self.root).create_child(name=stage, run_type="llm" if kind == "model" else "tool",
                inputs={}, start_time=end - timedelta(seconds=duration), extra={"metadata": meta})
            run.post()
            run.end(outputs={}, end_time=end, error="RequestError" if detail.get("error") else None)
            run.patch()
        except Exception:
            pass

    def close(self, success):
        if self.root is not None:
            try:
                self.root.end(outputs={}, error=None if success else "JobFailed")
                self.root.patch()
                self.client.flush()
            except Exception:
                pass
