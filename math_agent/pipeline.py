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
