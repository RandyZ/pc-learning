"""Invocation-scoped callbacks. Not serialized or checkpointed."""
from dataclasses import dataclass
from typing import Callable

@dataclass(frozen=True)
class LoopContext:
    request: Callable
    tools: list | None
    dispatch: Callable | None
    audit: Callable
    stage: str
    error_type: type[Exception]
    remaining_calls: Callable | None = None
    verify_initial_output: bool = False
    remaining_seconds: Callable | None = None
    max_turns: int | None = 4
