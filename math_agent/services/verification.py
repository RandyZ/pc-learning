"""One bounded mathematical process lane; exact, task-local receipt reuse."""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import time

from ..config import ROOT
from ..domain.v2 import digest
from ..infrastructure.v2_math import VERSION
from ..policies.v2 import operations, dependency_fingerprints, closure


def backend(requests, timeout=12):
    try:
        proc = subprocess.run([sys.executable, "-m", "math_agent.infrastructure.v2_math", str(max(1, math.ceil(timeout))), "512"],
            input=json.dumps(requests, ensure_ascii=False, allow_nan=False), text=True,
            capture_output=True, timeout=timeout, cwd=ROOT)
        if proc.returncode or len(proc.stdout) > 200000:
            raise ValueError("bounded computation process failed")
        result = json.loads(proc.stdout)
        if not isinstance(result, list) or len(result) != len(requests):
            raise ValueError("invalid computation batch response")
        return result
    except (subprocess.TimeoutExpired, ValueError) as exc:
        status = "timeout" if isinstance(exc, subprocess.TimeoutExpired) else "error"
        return [{"execution_status": status, "verdict": "unknown", "scope": "calculation",
                 "result": {"error_type": type(exc).__name__}} for _ in requests]


class ReceiptExecutor:
    def __init__(self, audit, check_deadline, remaining_seconds, *, reuse=False, execute=backend):
        self.audit, self.check = audit, check_deadline
        self.remaining_seconds, self.reuse, self.execute = remaining_seconds, reuse, execute
        self.cache = {}
        import sympy
        self.version = VERSION + ":" + sympy.__version__ + ":" + hashlib.sha256(
            (ROOT / "math_agent/infrastructure/v2_math.py").read_bytes()
            + (ROOT / "math_agent/tool_math.py").read_bytes()).hexdigest()

    def run(self, source_revision, revision, draft, selected=None, initial=None):
        by_id = operations(draft)
        required = set(by_id) if selected is None else set(selected)
        dependencies = {k: op.depends_on for k, (_, op) in by_id.items()}
        required = closure(required, dependencies)
        receipts = copy.deepcopy(initial or {})
        for oid, receipt in receipts.items():
            if oid not in by_id or receipt["source_revision"] != source_revision or receipt["candidate_revision"] != revision:
                raise ValueError("stale initial computation evidence")
        pending = required - set(receipts)
        while pending:
            self.check()
            ready = sorted(i for i in pending if set(dependencies[i]) <= set(receipts))[:12]
            if not ready:
                raise ValueError("cyclic or incomplete execution plan")
            requests, work = [], []
            waiting = {}
            for oid in ready:
                qid, op = by_id[oid]
                inputs, invalid = {}, False
                for name, parent in op.input_refs.items():
                    r = receipts[parent]
                    if r["execution_status"] != "ok" or not r["result"].get("scalar"):
                        invalid = True
                    else:
                        inputs[name] = r["result"]["exact"]
                signature = {"version": self.version, "source": source_revision,
                    "proof_dependencies": dependency_fingerprints(draft, qid),
                    "operation": op.model_dump(exclude={"id", "target_step_id", "input_refs", "depends_on"}),
                    "inputs": inputs, "dependencies": {d: receipts[d]["cache_key"] for d in dependencies[oid]}}
                key = digest(signature)
                if invalid:
                    value = {"execution_status": "unsupported", "verdict": "unknown", "scope": "calculation",
                             "result": {"error": "A required dependency did not produce a valid scalar"}}
                    receipts[oid] = self._receipt(oid, qid, op, source_revision, revision, key, value, 0)
                elif self.reuse and key in self.cache:
                    original = self.cache[key]
                    receipts[oid] = self._receipt(oid, qid, op, source_revision, revision, key, original, 0,
                                                   reused_from=original["receipt_id"])
                elif self.reuse and key in waiting:
                    waiting[key].append((oid, qid, op))
                else:
                    requests.append({"operation": op.model_dump(), "inputs": inputs})
                    work.append((oid, qid, op, key))
                    waiting[key] = []
            if requests:
                start = time.monotonic()
                values = self.execute(requests, timeout=max(.01, min(12., self.remaining_seconds())))
                seconds = time.monotonic() - start
                self.audit("v2_math_batch", {"stage": "check_math", "seconds": seconds, "operations": len(requests)})
                self.check()  # Late local work is not treated as a completed stage.
                if len(values) != len(work):
                    raise ValueError("incomplete mathematical batch")
                for (oid, qid, op, key), value in zip(work, values):
                    receipt = self._receipt(oid, qid, op, source_revision, revision, key, value, seconds)
                    receipts[oid] = receipt
                    if self.reuse and value["execution_status"] == "ok" and value["verdict"] in {"pass", "fail"}:
                        self.cache[key] = copy.deepcopy(receipt)
                    for duplicate, dqid, dop in waiting[key]:
                        # Reuse the same actual observation, including negative or unknown evidence.
                        receipts[duplicate] = self._receipt(duplicate, dqid, dop, source_revision, revision,
                            key, value, 0, reused_from=receipt["receipt_id"])
            pending -= set(ready)
        return receipts

    def _receipt(self, oid, qid, operation, source, revision, key, value, duration, reused_from=None):
        receipt = {"schema_version": "receipt-v2.1", "receipt_id": digest([revision, oid, key]),
            "operation_id": oid, "question_id": qid, "source_revision": source,
            "candidate_revision": revision, "producer": "sympy", "producer_version": self.version,
            "cache_key": key, "execution_status": value["execution_status"], "verdict": value["verdict"],
            "scope": value["scope"], "result": copy.deepcopy(value["result"]),
            "assumptions": operation.variables, "domains": {k: v.model_dump() for k, v in operation.domains.items()},
            "basis_ids": operation.basis_ids, "batch_duration_seconds": round(duration, 6),
            "reused_from": reused_from}
        self.audit("v2_receipt", {"stage": "check_math", "operation_id": oid, "receipt_id": receipt["receipt_id"],
            "execution_status": receipt["execution_status"], "verdict": receipt["verdict"],
            "reused_from": reused_from, "candidate_revision": revision})
        return receipt
