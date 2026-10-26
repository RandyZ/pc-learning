"""Stable entrypoint: classic compatibility implementation or opt-in V2 workflow.

The reviewed classic implementation remains unchanged in legacy_pipeline.py.
V2 planning/execution/compilation/publishing live in dedicated packages; source
extraction and the legacy evidence/adjudication gates are shared during migration.
"""
from .legacy_pipeline import (
    Runner as ClassicRunner, NeedsInput, canonical_page, review_payload,
    compact_tool_evidence, route, failed_inspection_detail,
)


class Runner(ClassicRunner):
    def __init__(self, settings, job_id, *args, **kwargs):
        super().__init__(settings, job_id, *args, **kwargs)
        if getattr(settings, "diagnostic_mode", False):
            if self.request.get("force") is not True or settings.redis_url:
                raise ValueError("Diagnostic mode requires a fresh, cache-disabled benchmark task")
            from .parallel_budget import ParallelBudget
            slots = self.shared_budget.max_concurrency if self.shared_budget is not None else 1
            self.shared_budget = ParallelBudget(settings.max_calls, max_concurrency=slots)
            self.provider.shared_budget = self.shared_budget
            self.audit("diagnostic_policy", {"stage": "setup", "job_deadline_seconds": None,
                "output_limit_policy": "provider_default", "http_timeout_seconds": settings.http_timeout,
                "max_calls": settings.max_calls, "max_repairs": settings.max_repairs,
                "note": "Diagnostic only; provider defaults and safety limits remain; no paid retry."})

    def audit(self, kind, detail):
        super().audit(kind, detail)
        trace = getattr(self, "trace_sink", None)
        if trace is not None:
            trace.completed(kind, detail)

    def execute(self):
        if self.settings.workflow == "evidence_v2":
            from .application.job_service import execute
            return execute(self)
        return super().execute()
