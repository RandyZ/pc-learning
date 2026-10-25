"""Stable deployment/import entrypoint, delegating to independently testable services.

No legacy Runner copy or business-stage implementation is kept here.
"""
from __future__ import annotations
from math_agent.application.context import JobContext
from math_agent.domain.errors import NeedsInput
from math_agent.domain.presentation import canonical_page, review_payload, compact_tool_evidence, route, failed_inspection_detail
from math_agent.models.gateway import ask as _ask
from math_agent.services.tool_execution import dispatch_stage_tool as _dispatch_stage_tool
from math_agent.services.validation import validated as _validated
from math_agent.services.source import extract as _extract, complete_ocr as _complete_ocr
from math_agent.services.evidence import save_ledger as _save_ledger, validate_parallel_candidate as _validate_parallel_candidate
from math_agent.services.solving import solve as _solve, solve_compact as _solve_compact, finish_compact as _finish_compact, repair_math as _repair_math
from math_agent.services.observation import observe as _observe
from math_agent.services.reviewing import review as _review, _review as service_review_internal
from math_agent.services.adjudication import settle_deferred as _settle_deferred, adjudicate as _adjudicate
from math_agent.application.job_service import execute as _execute, resume_review as _resume_review, run as _run

class Runner(JobContext):
    def ask(self, stage, payload=None, request_override=None):
        with self.trace_sink.span(stage):
            return _ask(self, stage, payload, request_override)

    def dispatch_stage_tool(self, stage, active_request, name, args):
        with self.trace_sink.span("tool"):
            return _dispatch_stage_tool(self, stage, active_request, name, args)

    def validated(self, stage, payload, validate, request_override=None):
        return _validated(self, stage, payload, validate, request_override)

    def extract(self, feedback=None):
        return _extract(self, feedback)

    def complete_ocr(self):
        return _complete_ocr(self)

    def save_ledger(self, source, candidate=None):
        return _save_ledger(self, source, candidate)

    def validate_parallel_candidate(self, source, candidate):
        return _validate_parallel_candidate(self, source, candidate)

    def solve(self, source, feedback=None, previous=None, stage='solve', _whole=False, allowed_tabs=None):
        return _solve(self, source, feedback, previous, stage, _whole, allowed_tabs)

    def solve_compact(self, source, feedback=None):
        return _solve_compact(self, source, feedback)

    def finish_compact(self, raw, source, feedback=None, require_basis=False, dependency_plan=None):
        return _finish_compact(self, raw, source, feedback, require_basis, dependency_plan)

    def repair_math(self, source, previous, feedback):
        return _repair_math(self, source, previous, feedback)

    def observe(self, candidate):
        return _observe(self, candidate)

    def review(self, source, candidate, observation, stage='review'):
        return _review(self, source, candidate, observation, stage)

    def _review(self, source, candidate, observation, stage='review', delta=None, math_scope=None):
        return service_review_internal(self, source, candidate, observation, stage, delta, math_scope)

    def settle_deferred(self, source, candidate, observation, review):
        return _settle_deferred(self, source, candidate, observation, review)

    def adjudicate(self, source, candidate, observation, failures, original_review=None):
        return _adjudicate(self, source, candidate, observation, failures, original_review)

    def execute(self):
        with self.trace_sink.span("job"):
            return _execute(self)

    def resume_review(self):
        return _resume_review(self)

    def run(self):
        from math_agent.observability.configuration import effective
        from math_agent.observability.timing import summarize
        from math_agent.storage import write_json
        write_json(self.directory / "effective-config.json", effective(self.settings))
        try:
            return _run(self)
        finally:
            # Sidecar diagnostics must never change the publication result.
            try:
                import json
                report = json.loads((self.directory / "report.json").read_text())
                write_json(self.directory / "perf.json", summarize(report))
            except (OSError, ValueError, TypeError):
                pass
