"""Serializable classic workflow data; never clients, locks or credentials."""
from typing import TypedDict
from math_agent.contracts import Source, Candidate, Review

class State(TypedDict, total=False):
    source: Source
    candidate: Candidate
    review: Review
    observation: dict
    feedback: list[dict]
    repairs: int
    next: str
    entry: str
