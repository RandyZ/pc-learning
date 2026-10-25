"""Per-task runtime resources. This object never enters LangGraph State."""
from __future__ import annotations
import copy
import json
import time
import threading
from pathlib import Path
from math_agent.cache import Cache
from math_agent.provider import DeepSeek
from math_agent.rendering import render
from math_agent.storage import Store, write_json
from math_agent.scene_tools import SceneTools
from math_agent.image_tools import ImageTools

class JobContext:

    def __init__(self, settings, job_id, provider_factory=DeepSeek, cache_factory=Cache, renderer=render):
        self.settings, self.job_id = (settings, job_id)
        self.store = Store(settings.data_dir)
        self.directory = self.store.path(job_id)
        self.request = json.loads((self.directory / 'request.json').read_text())
        self.records = []
        self.started = time.monotonic()
        self._audit_lock = threading.RLock()
        from math_agent.observability.tracing import TraceSink
        self.trace_sink = TraceSink.from_env()
        self.provider_factory = provider_factory
        self.question_plan_metadata = None
        self.pending_ocr = None
        self.shared_budget = None
        self.parallel_review_rounds = []
        if settings.parallel_questions or settings.parallel_review:
            from math_agent.parallel_budget import ParallelBudget
            self.shared_budget = ParallelBudget(settings.max_calls, deadline=self.started + settings.job_timeout, max_concurrency=2 if settings.parallel_review else settings.question_concurrency)
        self.provider = provider_factory(settings, self.audit)
        if self.shared_budget is not None:
            self.provider.shared_budget = self.shared_budget
        self.cache = cache_factory(settings, self.audit)
        self.renderer = renderer
        self.scenes = SceneTools(self.directory / 'observations')
        self.observation_count = 0
        self.source_rechecked = False
        self.resume_state = None
        self.ledger = {'entries': {}}
        self.image_tools = ImageTools(self.directory, self.request['images'])
        self.review_candidate = None
        self.review_snapshot = None
        self.review_certificates = []
        self.math_review_receipts = []
        self.review_change_tabs = None
        self.active_review_delta = None
        from math_agent.deferred_review import PendingReviews
        self.deferred_reviews = PendingReviews()
        pending_path = self.directory / 'deferred-reviews.json'
        if pending_path.exists():
            self.deferred_reviews.load(json.loads(pending_path.read_text()))
        import hashlib
        package = Path(__file__).resolve().parents[1]
        write_json(self.directory / 'runtime-manifest.json', {'package_root': str(package), 'python_files': {str(p.relative_to(package)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(package.rglob('*.py'))}})

    def audit(self, kind, detail):
        with self._audit_lock:
            entry = {'kind': kind, **copy.deepcopy(detail)}
            if 'seconds' in detail:
                entry['duration_seconds'] = detail['seconds']
            entry['seconds'] = round(time.monotonic() - self.started, 3)
            self.records.append(entry)
            write_json(self.directory / 'audit.json', self.records)
            self.store.event(self.job_id, detail.get('stage', kind), json.dumps(detail, ensure_ascii=False)[:1800])

        self.trace_sink.completed(kind, detail)

    @property
    def model_calls(self):
        return self.shared_budget.calls if self.shared_budget is not None else self.provider.calls
