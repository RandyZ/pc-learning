"""Publication checks are separate from solving and never initiate model work."""
from __future__ import annotations

import copy
import json
import time
from ..domain.v2 import digest, Draft
from ..contracts import Source, Candidate, Review
from ..storage import write_json
from ..observability.timing import summarize
from .reviewing import publication_gate

CACHE_FIELDS = ("source", "source_revision", "draft", "candidate_revision", "candidate", "evidence_bundle",
                "review", "operation_resolution", "prior_evidence", "repairs")


class Publisher:
    def __init__(self, runner, input_hash, cache_key):
        self.runner, self.input_hash, self.cache_key = runner, input_hash, cache_key
        self.cache_hit = False
        self.cached_tool_evidence = []

    def __call__(self, state):
        r = self.runner
        source, candidate = Source.model_validate(state["source"]), Candidate.model_validate(state["candidate"])
        review = Review.model_validate(state["review"])
        publication_gate(source, candidate, Draft.model_validate(state["draft"]), review,
            state["evidence_bundle"], state["operation_resolution"], state["source_revision"],
            state["candidate_revision"], r.deferred_reviews.pending)
        output = r.directory / "output"
        render_check = json.loads((output / "export-check.json").read_text())
        if render_check.get("status") != "pass" or digest(json.loads((output / "spec.json").read_text())) != digest(candidate.spec):
            raise ValueError("published page is not bound to the reviewed candidate")
        if not (output / "result.html").is_file() or not (output / "preview.png").is_file():
            raise ValueError("complete browser artifacts are missing")
        r.shared_budget.check()
        for name in ("source", "candidate", "review"):
            write_json(r.directory / (name + ".json"), state[name])
        write_json(r.directory / "v2-operation-resolution.json", state["operation_resolution"])
        write_json(r.directory / "v2-evidence.json", state["evidence_bundle"])
        report = {"status": "completed", "runtime": "langgraph", "workflow": "evidence_v2",
            "execution_profile": r.settings.v2_profile, "model": r.settings.model,
            "cache_hit": self.cache_hit, "model_calls_this_job": r.model_calls,
            "seconds": round(time.monotonic() - r.started, 3), "source": state["source"], "review": state["review"],
            "candidate_revision": state["candidate_revision"], "audit": r.records,
            "review_certificates": [], "math_review_receipts": [],
            "deferred_reviews": r.deferred_reviews.dump(), "render_check": render_check,
            "cached_tool_evidence": self.cached_tool_evidence,
            "limitations": ["No live-model performance or mathematical accuracy claim follows from offline tests.",
                "Symbolic results depend on declared assumptions; finite browser sampling is not a general proof.",
                "The first V2 release retains full independent review and rendering after patches."]}
        r.shared_budget.check()
        write_json(r.directory / "report.json", report)
        write_json(r.directory / "perf.json", summarize(report))
        r.store.finish(r.job_id, "completed", summary={k: report[k] for k in ("model", "cache_hit", "model_calls_this_job", "seconds", "workflow")})

        if not self.cache_hit:
            saved = {k: copy.deepcopy(state[k]) for k in CACHE_FIELDS if k in state}
            r.cache.put(self.cache_key, {"schema_version": "v2-cache.1", "input_hash": self.input_hash,
                "state": saved, "state_hash": digest(saved),
                "tool_evidence": [x for x in r.records if x["kind"] == "tool"]})
