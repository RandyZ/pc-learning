from typing import TypedDict

class LoopState(TypedDict):
    history: list[dict]
    message: dict
    finish: str
    turns: int
    value: dict
    finalizing: bool
