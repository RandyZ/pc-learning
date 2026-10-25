"""Serializable graph state. Credentials, images and live clients stay in context."""
from typing import TypedDict


class State(TypedDict, total=False):
    job_id: str
    studio_nonce: str
    source: dict
    source_revision: str
    draft: dict
    candidate_revision: str
    prerequisites: dict
    math_evidence: dict
    diagram_evidence: dict
    candidate: dict
    evidence_bundle: dict
    review: dict
    operation_resolution: dict
    feedback: list[dict]
    prior_evidence: list[dict]
    repairs: int
    route: str
    completed: bool
