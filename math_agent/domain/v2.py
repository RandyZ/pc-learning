"""Versioned V2 contracts. No model, graph, database or browser clients live here."""
from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from ..jsxgraph.spec import Geometry, Controls, View, Scalar, Pair

ID = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,31}$")]
Text = Annotated[str, Field(min_length=1, max_length=3000)]
Expression = Annotated[str, Field(min_length=1, max_length=600)]


class Data(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


def digest(value) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False, separators=(",", ":")).encode()).hexdigest()


class ProofStep(Data):
    id: ID
    premise: Text
    rule: Text
    conclusion: Text
    basis_ids: list[str] = Field(min_length=1, max_length=30)


class Proof(Data):
    result: Text
    steps: list[ProofStep] = Field(min_length=1, max_length=40)
    cases: list[Text] = Field(default_factory=list, max_length=30)
    domain_and_boundaries: Text
    condition_ids: list[str] = Field(min_length=1, max_length=100)


class Interval(Data):
    lower: float
    upper: float
    lower_inclusive: bool = True
    upper_inclusive: bool = True

    @model_validator(mode="after")
    def ordered(self):
        if self.lower >= self.upper:
            raise ValueError("interval requires lower < upper")
        return self


class Operation(Data):
    id: ID
    op: Literal["simplify", "evaluate", "solve", "equivalent", "substitute_check", "endpoints"]
    target_step_id: ID
    basis_ids: list[str] = Field(min_length=1, max_length=30)
    expression: Expression | None = None
    expected: Expression | None = Field(default=None, description=(
        "Required for equivalent/substitute_check; optional for simplify/evaluate. "
        "When provided it is an equality assertion that is checked, never ignored. "
        "Not supported for solve/endpoints."))
    equations: list[Expression] = Field(default_factory=list, max_length=6)
    variables: dict[ID, Literal["real", "positive", "nonnegative", "nonzero"]] = Field(
        default_factory=dict, max_length=4, description=(
            'Final computation plan: name-to-assumption object, e.g. {"x":"real"}; '
            'NOT the variables array used by the separate algebra_batch tool. '
            'Stronger assumptions require source/proof support; do not invent them.'))
    substitutions: dict[ID, Scalar] = Field(default_factory=dict, max_length=4, description=(
        "evaluate: omit for constant expressions; substitute every declared variable "
        "when variables are present. substitute_check: at least one explicit substitution. "
        "Other operations do not accept substitutions."))
    domains: dict[ID, Interval] = Field(default_factory=dict, max_length=4)
    input_refs: dict[ID, ID] = Field(default_factory=dict, max_length=8)
    depends_on: list[ID] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def valid_shape(self):
        if self.op == "solve":
            if not self.variables or not self.equations or self.expression is not None:
                raise ValueError("solve requires variables/equations, not expression")
        elif self.expression is None or self.equations:
            raise ValueError("this operation requires expression, not equations")
        if self.op in {"equivalent", "substitute_check"} and self.expected is None:
            raise ValueError("equivalent/substitute_check require expected")
        if self.op in {"solve", "endpoints"} and self.expected is not None:
            raise ValueError("expected is not supported for solve/endpoints")
        if self.op == "substitute_check" and not self.substitutions:
            raise ValueError("substitute_check requires explicit substitutions")
        if self.op == "evaluate" and set(self.variables) - set(self.substitutions):
            raise ValueError("evaluate requires substitutions for every declared variable; "
                             "constant expressions need no substitutions")
        if self.op not in {"evaluate", "substitute_check"} and self.substitutions:
            raise ValueError("substitutions are not accepted for this operation")
        if not set(self.substitutions) <= set(self.variables) or not set(self.domains) <= set(self.variables):
            raise ValueError("substitutions/domains must name declared variables")
        if set(self.variables) & set(self.input_refs):
            raise ValueError("input reference must not shadow a variable")
        if self.op == "endpoints" and len(self.domains) != 1:
            raise ValueError("endpoints requires exactly one explicit interval")
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("duplicate operation dependency")
        if not set(self.input_refs.values()) <= set(self.depends_on):
            raise ValueError("every input_ref must be declared in depends_on")
        return self


class Transform(Data):
    id: Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,19}$")]
    op: Literal["rotate_polygon", "translate_polygon", "reflect_group"]
    vertices: list[ID] = Field(min_length=1, max_length=12)
    output_vertices: list[ID] = Field(min_length=1, max_length=12)
    basis_ids: list[str] = Field(min_length=1, max_length=30)
    labels: dict[ID, str] = Field(default_factory=dict, max_length=12)
    center: ID | None = None
    direction: Literal["clockwise", "counterclockwise"] | None = None
    degrees: Scalar | None = None
    vector: Pair | None = None
    mirror_line: ID | None = None
    connect: bool = True

    @model_validator(mode="after")
    def semantics(self):
        if len(self.vertices) != len(self.output_vertices):
            raise ValueError("a transformed vertex must have exactly one output")
        if len(set(self.vertices)) != len(self.vertices) or len(set(self.output_vertices)) != len(self.output_vertices):
            raise ValueError("duplicate transform vertex")
        if not set(self.labels) <= set(self.output_vertices):
            raise ValueError("labels may name only output vertices")
        if self.connect and len(self.vertices) < 3:
            raise ValueError("closed polygon requires at least three vertices")
        if self.op == "rotate_polygon":
            if self.center is None or self.direction is None or self.degrees is None or self.vector is not None or self.mirror_line is not None:
                raise ValueError("rotation requires center/direction/degrees only")
        elif self.op == "translate_polygon":
            if self.vector is None or any(v is not None for v in (self.center, self.direction, self.degrees, self.mirror_line)):
                raise ValueError("translation requires vector only")
        elif self.mirror_line is None or any(v is not None for v in (self.center, self.direction, self.degrees, self.vector)):
            raise ValueError("reflection requires mirror_line only")
        return self


class MovingPoint(Data):
    id: ID
    op: Literal["function_with_moving_point"]
    point_id: ID
    expression: Expression
    domain: Annotated[list[float], Field(min_length=2, max_length=2)]
    basis_ids: list[str] = Field(min_length=1, max_length=30)
    label: str = Field(default="", max_length=50)
    source_names: list[str] = Field(default_factory=list, max_length=12)


class ScenePlan(Data):
    objects: list[Geometry] = Field(default_factory=list, max_length=120)
    constructions: list[Transform | MovingPoint] = Field(default_factory=list, max_length=20)
    view: View | None = None
    viewport: Literal["manual", "fit_points"] = "fit_points"
    axis: bool = True
    controls: Controls | None = None
    required_visible: list[ID] = Field(default_factory=list, max_length=100)
    step_links: dict[ID, list[ID]] = Field(default_factory=dict, max_length=40)

    @model_validator(mode="after")
    def not_empty(self):
        if not self.objects and not self.constructions:
            raise ValueError("scene must contain at least one object or construction")
        if self.viewport == "manual" and self.view is None:
            raise ValueError("manual viewport requires an explicit view")
        return self


class QuestionDraft(Data):
    proof: Proof
    computations: list[Operation] = Field(default_factory=list, max_length=24)
    scene: ScenePlan


class Draft(Data):
    schema_version: Literal["evidence-v2.1"] = "evidence-v2.1"
    questions: dict[str, QuestionDraft] = Field(min_length=1, max_length=12)


class QuestionUpdate(Data):
    proof: Proof | None = None
    computations: list[Operation] | None = Field(default=None, max_length=24)
    scene: ScenePlan | None = None

    @model_validator(mode="after")
    def nonempty(self):
        if all(getattr(self, k) is None for k in ("proof", "computations", "scene")):
            raise ValueError("empty patch")
        return self


class ChangeSet(Data):
    expected_candidate_revision: Annotated[str, Field(pattern="^[0-9a-f]{64}$")]
    reason: Text
    updates: dict[str, QuestionUpdate] = Field(min_length=1, max_length=12)


class OperationReview(Data):
    verdict: Literal["pass", "issue", "unknown"]
    method: Literal["receipt", "independent_proof", "unresolved"]
    basis_ids: list[str] = Field(min_length=1, max_length=30)
    evidence: Text


class RevisionReview(Data):
    verdict: Literal["resolved", "issue", "unknown"]
    basis_ids: list[str] = Field(min_length=1, max_length=30)
    evidence: Text


def prior_issues(bundle):
    return {f"R{r+1}I{i+1}": issue
            for r, row in enumerate(bundle.get("prior_evidence", []))
            for i, issue in enumerate(row["issues"])}
