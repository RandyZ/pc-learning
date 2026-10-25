"""Dependencies supplied at invocation, not embedded in graph state."""
from dataclasses import dataclass
from math_agent.domain.ports import ClassicStages

@dataclass(frozen=True)
class ClassicContext:
    job: ClassicStages
