"""Mechanical ScenePlan -> JSXGraph compilation; never invent mathematical data."""
from __future__ import annotations

import ast
import copy
from ..contracts import Candidate, check_candidate
from ..domain.v2 import Draft
from ..policies.v2 import solutions, SCALAR_FIELDS
from ..jsxgraph.spec import validate, expression, POINTS

VERSION = "scene-v2.1"


class CompilationError(ValueError):
    def __init__(self, question_id, detail):
        self.issues = [{"stage": "diagram", "target": question_id, "detail": detail}]
        super().__init__(detail)


def _resolve(value, results, enabled=False):
    if isinstance(value, str) and enabled and value.startswith("@"):
        result = results.get(value[1:], {})
        if result.get("execution_status") != "ok" or not result.get("result", {}).get("scalar"):
            raise ValueError("scene needs a completed, finite real scalar: " + value)
        exact = result["result"]["exact"]
        expression(exact, set())  # Same allowlist as the renderer; no arbitrary code.
        return exact
    if isinstance(value, list):
        return [_resolve(v, results, enabled) for v in value]
    if isinstance(value, dict):
        return {k: _resolve(v, results, k in SCALAR_FIELDS) for k, v in value.items()}
    return value


def _at_parameter(text):
    expression(text, {"x"})
    tree = ast.parse(text.replace("^", "**"), mode="eval")
    class Bind(ast.NodeTransformer):
        def visit_Name(self, node):
            return ast.copy_location(ast.Name(id="t", ctx=ast.Load()), node) if node.id == "x" else node
    return ast.unparse(Bind().visit(tree))


def compile_scene(qid, scene, results):
    raw = _resolve(scene.model_dump(exclude_none=True), results)
    objects = copy.deepcopy(raw["objects"])
    mapping = {o["id"]: {"question_id": qid, "plan_node": "object:" + o["id"]} for o in objects}
    if len(mapping) != len(objects):
        raise CompilationError(qid, "duplicate base object ID")
    def add(obj, macro):
        if obj["id"] in mapping:
            raise ValueError("generated object ID collision: " + obj["id"])
        objects.append(obj)
        mapping[obj["id"]] = {"question_id": qid, "plan_node": "construction:" + macro["id"],
                              "basis_ids": macro["basis_ids"]}
    try:
        for macro in raw["constructions"]:
            op = macro["op"]
            if op == "function_with_moving_point":
                slider = raw.get("controls", {}).get("slider")
                lo, hi = macro["domain"]
                if not slider or not lo <= slider["min"] < slider["max"] <= hi:
                    raise ValueError("moving point needs an explicit slider contained in the function domain")
                add({"id": macro["id"], "kind": "functiongraph", "expression": macro["expression"],
                     "domain": macro["domain"], "label": macro["label"], "source_names": macro["source_names"]}, macro)
                add({"id": macro["point_id"], "kind": "point", "label": macro["point_id"],
                     "coords": ["t", _at_parameter(macro["expression"])]}, macro)
                continue
            for before, after in zip(macro["vertices"], macro["output_vertices"]):
                obj = {"id": after, "label": macro["labels"].get(after, after)}
                if op == "rotate_polygon":
                    obj.update(kind="rotation", parents=[before, macro["center"]],
                               direction=macro["direction"], degrees=macro["degrees"])
                elif op == "translate_polygon":
                    obj.update(kind="translation", parents=[before], vector=macro["vector"])
                elif op == "reflect_group":
                    obj.update(kind="reflection", parents=[before, macro["mirror_line"]])
                else:
                    raise ValueError("unsupported macro")
                add(obj, macro)
            if macro["connect"]:
                # Explicitly show all original and image edges; no invented coordinates.
                for side, ids in (("src", macro["vertices"]), ("dst", macro["output_vertices"])):
                    for i, a in enumerate(ids):
                        b = ids[(i + 1) % len(ids)]
                        existing = next((o for o in objects if o["kind"] == "segment"
                                         and set(o["parents"]) == {a, b} and o.get("visible", True)), None)
                        if not existing:
                            add({"id": f"{macro['id']}_{side}{i}", "kind": "segment", "parents": [a, b]}, macro)
        by_id = {o["id"]: o for o in objects}
        required = set(raw["required_visible"]) | {i for ids in raw["step_links"].values() for i in ids}
        if any(i not in by_id or not by_id[i].get("visible", True) for i in required):
            raise ValueError("a required teaching object is missing or hidden")
        if raw["viewport"] == "fit_points" and not any(o["kind"] in POINTS and o.get("visible", True) for o in objects):
            raise ValueError("a scene without visible points needs an explicit manual view")
        board = {"axis": raw["axis"], "viewport": raw["viewport"], "objects": objects}
        if "view" in raw:
            board["view"] = raw["view"]
        return {"board": board, **({"controls": raw["controls"]} if "controls" in raw else {})}, mapping
    except ValueError as exc:
        raise CompilationError(qid, str(exc)) from exc


def compile_candidate(source, draft: Draft, results):
    answer = solutions(source, draft)
    tabs, source_map = [], {}
    for q, solution in zip(source.questions, answer):
        tab, mapping = compile_scene(q.id, draft.questions[q.id].scene, results)
        tab.update(tab_title=q.id, steps=[{"head": s.rule, "content": s.premise + "\n" + s.conclusion} for s in solution.steps],
                   conclusion=solution.result)
        if solution.cases:
            tab["steps"].append({"head": "分类讨论", "content": "\n".join(solution.cases)})
        tab["steps"].append({"head": "定义域与边界检查", "content": solution.domain_and_boundaries})
        try:
            # Validate the full native schema and references, not only macro input.
            validate({"format": "jsxgraph-v1", "title": source.title, "problem_text": source.problem_text, "tabs": [tab]})
        except ValueError as exc:
            raise CompilationError(q.id, str(exc)) from exc
        tabs.append(tab)
        source_map[q.id] = mapping
    spec = validate({"format": "jsxgraph-v1", "title": source.title, "problem_text": source.problem_text, "tabs": tabs})
    candidate = Candidate(solutions=answer, spec=spec)
    check_candidate(candidate, source)
    from ..native_constructions import require
    from ..explanation_coverage import require as require_explanation
    require(source, candidate)
    require_explanation(candidate)
    return candidate, {"compiler_version": VERSION, "objects": source_map}
