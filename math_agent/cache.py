from __future__ import annotations
import hashlib
import json
from .config import ROOT
from .prompts import VERSION


def key_for(settings, request):
    # Renderer implementation deliberately excluded: cached model data is revalidated and rendered anew.
    context = {"version": VERSION, "image_transport_hash":hashlib.sha256((ROOT / "math_agent/image_transport.py").read_bytes()).hexdigest(), "deferred_review_hash":hashlib.sha256((ROOT / "math_agent/deferred_review.py").read_bytes()+(ROOT / "math_agent/math_patch.py").read_bytes()+(ROOT / "math_agent/parent_binding.py").read_bytes()+(ROOT / "math_agent/proof_guards.py").read_bytes()+(ROOT / "math_agent/rotation_probe.py").read_bytes()).hexdigest(), "review_responses":settings.review_responses,"solve_responses":settings.solve_responses, "responses_transport_hash":hashlib.sha256((ROOT / "math_agent/responses_transport.py").read_bytes()).hexdigest(), "incremental_review":settings.incremental_review, "incremental_math_review":settings.incremental_math_review, "math_review_scope_hash":hashlib.sha256((ROOT / "math_agent/math_review_scope.py").read_bytes()+(ROOT / "math_agent/review_partition.py").read_bytes()).hexdigest(), "review_reuse_hash":hashlib.sha256((ROOT / "math_agent/review_reuse.py").read_bytes()).hexdigest(), "stage_budgets":[settings.solve_max_tokens,settings.review_max_tokens,settings.review_reserve_seconds], "compact_tools":settings.compact_tools, "compact_contract":hashlib.sha256((ROOT / "math_agent/compact_output.py").read_bytes()+(ROOT / "math_agent/submission.py").read_bytes()+(ROOT / "math_agent/explanation_coverage.py").read_bytes()+(ROOT / "math_agent/compact_review.py").read_bytes()+(ROOT / "math_agent/compact_adjudication.py").read_bytes()+(ROOT / "math_agent/diagram_patch.py").read_bytes()+(ROOT / "math_agent/solve_views.py").read_bytes()).hexdigest(), "native_constructions":settings.native_constructions,"native_constructions_hash":hashlib.sha256((ROOT / "math_agent/native_constructions.py").read_bytes()+(ROOT / "math_agent/contracts.py").read_bytes()+(ROOT / "math_agent/point_names.py").read_bytes()).hexdigest(), "reference_mode":settings.reference_mode, "review_plan":settings.review_plan, "solve_draft":settings.solve_draft, "reference_flow":hashlib.sha256((ROOT / "math_agent/reference_flow.py").read_bytes()+(ROOT / "math_agent/reference_prompts.py").read_bytes()).hexdigest(), "prompts_hash": hashlib.sha256((ROOT / "math_agent/prompts.py").read_bytes()).hexdigest(), "model": settings.model, "base_url": settings.base_url,
        "overlap_ocr":settings.overlap_ocr,"ocr_overlap_hash":hashlib.sha256((ROOT/"math_agent/ocr_overlap.py").read_bytes()).hexdigest(),"ocr_url": settings.ocr_url, "ocr_profile": settings.ocr_profile, "ocr_hash": hashlib.sha256((ROOT / "math_agent/ocr.py").read_bytes()).hexdigest(), "source_max_tokens": settings.source_max_tokens, "thinking": settings.thinking, "diagram_thinking": settings.diagram_thinking, "effort": settings.effort, "max_tokens": settings.max_tokens,
        "text": request["text"], "images": [{"id": i["id"], "role": i["role"], "sha256": i["sha256"]} for i in request["images"]],
        "contracts": hashlib.sha256((ROOT / "math_agent/contracts.py").read_bytes() + (ROOT / "math_agent/source_review.py").read_bytes() + (ROOT / "math_agent/text_evidence.py").read_bytes() + (ROOT / "math_agent/jsxgraph/spec.py").read_bytes()).hexdigest(),
        "tools": hashlib.sha256((ROOT / "math_agent/tools.py").read_bytes() + (ROOT / "math_agent/tool_math.py").read_bytes() + (ROOT / "math_agent/symbolic_geometry.py").read_bytes() + (ROOT / "math_agent/math_knowledge.json").read_bytes() + (ROOT / "math_agent/scene_tools.py").read_bytes() + (ROOT / "math_agent/evidence.py").read_bytes() + (ROOT / "math_agent/image_tools.py").read_bytes() + (ROOT / "math_agent/source_views.py").read_bytes() + (ROOT / "math_agent/inspection_recovery.py").read_bytes() + (ROOT / "math_agent/proof_dependencies.py").read_bytes()).hexdigest()}
    context['image_input_hash'] = hashlib.sha256((ROOT/'math_agent/image_input.py').read_bytes()).hexdigest()
    context['review_effort']=settings.review_effort
    context['provider_hash']=hashlib.sha256((ROOT/'math_agent/provider.py').read_bytes()).hexdigest()
    context['json_framing_hash']=hashlib.sha256((ROOT/'math_agent/json_framing.py').read_bytes()).hexdigest()
    context['function_consistency_hash']=hashlib.sha256((ROOT/'math_agent/function_consistency.py').read_bytes()).hexdigest()
    context['review_passages_hash']=hashlib.sha256((ROOT/'math_agent/review_passages.py').read_bytes()).hexdigest()
    context['parallel_review']={'enabled':settings.parallel_review,'hash':hashlib.sha256((ROOT/'math_agent/parallel_review.py').read_bytes()).hexdigest()}
    context['question_agents'] = {'enabled':settings.parallel_questions,'concurrency':settings.question_concurrency,
        'hash':hashlib.sha256(b''.join((ROOT/'math_agent'/f).read_bytes()
            for f in ('parallel_budget.py','question_plan.py','parallel_solve.py'))).hexdigest()}
    # The classic implementation now spans packages; invalidate evidence when any
    # migrated rule changes, without including browser assets or credentials.
    package = ROOT / "math_agent"
    moved = [package / name for name in ("pipeline.py", "flow.py", "agent_loop.py", "execution_limits.py")]
    for name in ("application", "domain", "models", "services", "workflows"):
        moved.extend(sorted((package / name).rglob("*.py")))
    context["classic_structure_hash"] = hashlib.sha256(b"".join(
        str(path.relative_to(package)).encode() + b"\0" + path.read_bytes()
        for path in moved)).hexdigest()
    from .execution_limits import validation, snapshot
    if validation(settings):
        context["validation_policy"] = snapshot(settings)
    return "math-agent:v1:" + hashlib.sha256(json.dumps(context, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class Cache:
    def __init__(self, settings, audit):
        self.audit, self.ttl = audit, settings.cache_ttl
        self.client = None
        if settings.redis_url:
            import redis
            self.client = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)

    def get(self, key):
        if not self.client:
            self.audit("cache", {"status": "disabled"})
            return None
        try:
            value = self.client.get(key)
            return json.loads(value) if value else None
        except Exception:
            # Do not silently bill for regeneration when the expected cache is unavailable.
            raise RuntimeError("Redis 缓存不可用，已停止任务；恢复后重新提交，避免意外重新计费") from None

    def put(self, key, value):
        if self.client:
            try: self.client.setex(key, self.ttl, json.dumps(value, ensure_ascii=False))
            except Exception: self.audit("cache", {"status": "write_failed", "detail": "本次结果保留，但下次可能无法复用"})
