"""Existing provider and budgets, with an explicit V2 evidence-review extension."""
from __future__ import annotations

import copy
import json
import time

from ..domain.v2 import OperationReview, RevisionReview, prior_issues


class ModelGateway:
    def __init__(self, inner, job_id, audit):
        self.inner, self.job_id, self.audit = inner, job_id, audit
        self.review_bundle = None
        self.operation_basis_map = None
        self.operation_reviews = None
        self.revision_reviews = None
        self.candidate_revision = None
        self._starts = {}
        # Transport events, not generate() calls, are the actual request count.
        self.inner.audit = self._transport_audit

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def _transport_audit(self, kind, detail):
        detail = copy.deepcopy(detail)
        call = detail.get("call")
        if call is not None:
            detail["request_id"] = f"{self.job_id}:{call}"
            detail["candidate_revision"] = self.candidate_revision
        if kind == "model_budget":
            self._starts[call] = time.monotonic()
            self.audit("model_request_started", {"stage": detail.get("stage"), "call": call,
                "request_id": detail["request_id"], "candidate_revision": self.candidate_revision})
        if kind == "model" and call in self._starts:
            detail.setdefault("seconds", time.monotonic() - self._starts.pop(call))
        self.audit(kind, detail)

    def generate(self, stage, messages, tools=None, dispatch=None, **kwargs):
        extend = stage in {"review", "diagram_review"} and self.review_bundle is not None
        if extend:
            messages = copy.deepcopy(messages)
            ids = sorted(self.review_bundle["receipts"])
            schema = {"type": "object", "additionalProperties": False, "required": ids,
                      "properties": {i: OperationReview.model_json_schema() for i in ids}}
            for oid in ids:
                schema["properties"][oid]["properties"]["basis_ids"]["items"]["enum"] = \
                    self.operation_basis_map[oid]["allowed_basis_ids"]
            messages[0]["content"] += (
                "\nV2复核额外输出operation_reviews对象，逐计算ID覆盖，不替换原有conditions/questions/issues。"
                "计算成功不证明其建模/假设正确；逐条对照题设与证明。receipt方式只用于实际已通过且范围适用的证据；"
                "unknown/error/unsupported必须保留unknown，或用independent_proof给出明确独立依据与完整理由。"
                "已发现的fail不得声称pass，须报告对应math异议。依据编号用当前公共C编号或Qn:Sm；"
                "不得删检查或将目标本身当依据。每项目标自身及后续步骤不可引用，逐项目标与允许依据："
                + json.dumps(self.operation_basis_map, ensure_ascii=False)
                + "。额外字段Schema：" + json.dumps(schema, ensure_ascii=False))
            prior = prior_issues(self.review_bundle)
            revision_schema = {"type": "object", "additionalProperties": False, "required": list(prior),
                "properties": {i: RevisionReview.model_json_schema() for i in prior}}
            if prior:
                messages[0]["content"] += ("\n同时输出revision_reviews，逐条解释旧异议在当前版本是否已解除；"
                    "不能因重新计算或更换检查表达式就默认解除旧反例。未解决返回issue/unknown。Schema："
                    + json.dumps(revision_schema, ensure_ascii=False) + "；异议编号：" + json.dumps(prior, ensure_ascii=False))
            messages.append({"role": "user", "content": "版本绑定的V2计算证据：" + json.dumps(self.review_bundle, ensure_ascii=False)})
            if kwargs.get("output_schema") is not None:
                output = copy.deepcopy(kwargs["output_schema"])
                output.setdefault("properties", {})["operation_reviews"] = schema
                output.setdefault("required", []).append("operation_reviews")
                if prior:
                    output["properties"]["revision_reviews"] = revision_schema
                    output["required"].append("revision_reviews")
                kwargs["output_schema"] = output
        raw = self.inner.generate(stage, messages, tools=tools, dispatch=dispatch, **kwargs)
        if extend:
            raw = copy.deepcopy(raw)
            self.operation_reviews = raw.pop("operation_reviews", None)
            self.revision_reviews = raw.pop("revision_reviews", {})
        return raw
