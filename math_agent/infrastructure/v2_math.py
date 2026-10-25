"""CPU/memory/time-bounded symbolic worker using the existing AST allowlist.

This process accepts data, never Python programs. A successful calculation is
not evidence that the model's formalization matches the source image.
"""
from __future__ import annotations

import ast
import json
import math
import sys

from ..domain.v2 import Operation
from ..tool_math import expression

VERSION = "sympy-plan-v2.1"
RESERVED = {"pi", "PI", "sqrt", "root", "abs", "sin", "cos", "tan", "frac", "Rational", "SQRT2", "SQRT3"}


def evaluate(raw, inputs=None):
    import sympy as s
    op = Operation.model_validate(raw)
    inputs = inputs or {}
    if set(inputs) != set(op.input_refs) or (set(op.variables) | set(inputs)) & RESERVED:
        raise ValueError("invalid input symbols")
    symbols = {n: s.Symbol(n, **({"real": True} if a == "real" else {a: True, "real": True}))
               for n, a in op.variables.items()}
    constants = {n: expression(v, {}) for n, v in inputs.items()}
    if any(v.is_real is not True or not v.is_number or v.is_finite is not True for v in constants.values()):
        raise ValueError("dependency must be a finite real scalar")
    env = {**symbols, **constants}
    constraints = []

    def parse(text):
        denominators = []
        result = expression(text, env, domain_objects=denominators)
        constraints.extend((d, "nonzero") for d in denominators)
        # Preserve pre-simplification radical/trigonometric domain obligations.
        tree = ast.parse(str(text).replace("^", "**"), mode="eval")
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in {"sqrt", "root"} and len(node.args) == 1:
                    constraints.append((expression(ast.unparse(node.args[0]), env), "nonnegative"))
                elif node.func.id == "tan" and len(node.args) == 1:
                    constraints.append((s.cos(expression(ast.unparse(node.args[0]), env)), "nonzero"))
            elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
                exponent = expression(ast.unparse(node.right), env)
                base = expression(ast.unparse(node.left), env)
                if exponent.is_integer is not True:
                    # Conservative real power domain; do not silently cancel it away.
                    constraints.append((base, "positive"))
        return result

    def holds(value, relation):
        flag = getattr(value, {"nonzero": "is_nonzero", "nonnegative": "is_nonnegative", "positive": "is_positive"}[relation])
        if flag is not None:
            return flag
        name = str(value)
        interval = op.domains.get(name)
        if value in symbols.values() and interval is not None:
            lo, hi = interval.lower, interval.upper
            if relation == "nonzero":
                if lo > 0 or hi < 0 or lo == 0 and not interval.lower_inclusive or hi == 0 and not interval.upper_inclusive:
                    return True
            elif relation == "nonnegative" and lo >= 0:
                return True
            elif relation == "positive" and (lo > 0 or lo == 0 and not interval.lower_inclusive):
                return True
        return None

    for name, interval in op.domains.items():
        assumption = op.variables[name]
        if assumption in {"positive", "nonnegative"} and (interval.upper < 0 or interval.upper == 0):
            raise ValueError("declared assumptions contradict interval")
    substitutions = {symbols[n]: expression(v, {}) for n, v in op.substitutions.items()}
    for name, val in ((str(n), v) for n, v in substitutions.items()):
        if val.is_real is not True or not val.is_number or val.is_finite is not True:
            raise ValueError("substitution must be a finite real constant")
        a = op.variables[name]
        if a != "real" and holds(val, a) is not True:
            raise ValueError("substitution violates a declared assumption")
        d = op.domains.get(name)
        if d and not bool((val >= d.lower if d.lower_inclusive else val > d.lower)
                          & (val <= d.upper if d.upper_inclusive else val < d.upper)):
            raise ValueError("substitution outside declared interval")

    def restrictions(subs=None):
        unresolved = []
        for value, relation in constraints:
            value = s.simplify(value.subs(subs or {}, simultaneous=True))
            status = holds(value, relation)
            if status is False:
                raise ValueError("expression outside its real domain")
            if status is not True:
                unresolved.append(f"{value}: {relation}")
        return sorted(set(unresolved))

    def row(value, *, check=None, extra=None, unresolved=None):
        missing = restrictions(substitutions) if unresolved is None else unresolved
        if value is not None and (value.has(s.zoo, s.nan, s.oo, -s.oo) or value.is_real is False):
            raise ValueError("non-finite or non-real result")
        if value is not None and value.is_real is None:
            missing = sorted(set(missing + [f"{value}: real"]))
        result = {"domain_restrictions": missing, **(extra or {})}
        if value is not None:
            result.update(exact=str(value), scalar=bool(not missing and value.is_number and value.is_real is True and value.is_finite is True))
        verdict = "unknown" if missing else ("pass" if check is True else "fail" if check is False else "unknown")
        if op.op not in {"equivalent", "substitute_check"} and not missing:
            verdict = "pass"  # Calculation completed; scope explicitly isn't proof.
        return {"execution_status": "ok", "verdict": verdict,
                "scope": "symbolic_under_assumptions" if op.op in {"equivalent", "substitute_check"} else "calculation",
                "result": result}

    if op.op == "solve":
        eqs = []
        for eq in op.equations:
            parts = eq.split("=")
            if len(parts) > 2:
                raise ValueError("only one equals sign per equation")
            eqs.append(parse(parts[0]) - parse(parts[1] if len(parts) == 2 else "0"))
        roots = s.solve(eqs, list(symbols.values()), dict=True, check=True)
        return row(None, extra={"candidate_solutions": [{str(k): str(v) for k, v in r.items()} for r in roots],
                                "complete": False, "note": "Candidate roots only; source constraints and completeness require independent review."})
    value = parse(op.expression)
    if op.op == "endpoints":
        name, d = next(iter(op.domains.items()))
        values = []
        for edge, included in ((d.lower, d.lower_inclusive), (d.upper, d.upper_inclusive)):
            # An excluded endpoint is not automatically a legitimate substitution.
            if not included:
                values.append({"value": edge, "included": False, "status": "not_evaluated"})
                continue
            sub = {symbols[name]: s.Rational(str(edge))}
            unknown = restrictions(sub)
            calculated = s.simplify(value.subs(sub, simultaneous=True))
            if calculated.has(s.zoo, s.nan, s.oo, -s.oo) or calculated.is_real is False:
                raise ValueError("endpoint not in expression domain")
            values.append({"value": edge, "included": True, "exact": str(calculated), "domain_restrictions": unknown})
        return row(None, extra={"endpoints": values, "note": "Not an interval extremum or continuous-domain proof."})
    if substitutions:
        # Check the original domain before simplifying/substituting the expression.
        restrictions(substitutions)
        value = value.subs(substitutions, simultaneous=True)
    if op.op in {"equivalent", "substitute_check"}:
        expected = parse(op.expected).subs(substitutions, simultaneous=True)
        difference = s.simplify(value - expected)
        return row(difference, check=difference.is_zero, extra={"expected": str(expected), "claim": "equality under the declared assumptions"})
    return row(s.simplify(value))


def main():
    import resource
    seconds, memory_mb = int(sys.argv[1]), int(sys.argv[2])
    resource.setrlimit(resource.RLIMIT_CPU, (seconds + 1, seconds + 2))
    if sys.platform == "linux":
        resource.setrlimit(resource.RLIMIT_AS, (memory_mb * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024,) * 2)
    payload = sys.stdin.read(262145)
    if len(payload) > 262144:
        raise ValueError("batch too large")
    requests = json.loads(payload)
    if not isinstance(requests, list) or not 1 <= len(requests) <= 12:
        raise ValueError("batch must contain 1..12 operations")
    results = []
    for req in requests:
        try:
            results.append(evaluate(req["operation"], req.get("inputs")))
        except Exception as exc:
            results.append({"execution_status": "error", "verdict": "unknown", "scope": "calculation",
                            "result": {"error_type": type(exc).__name__, "error": str(exc)[:600]}})
    print(json.dumps(results, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
