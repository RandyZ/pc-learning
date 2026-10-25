"""Per-job dependencies and immutable input identity, outside the graph state."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import time
from ...domain.v2 import digest


@dataclass
class Context:
    runner: object
    solver: object
    executor: object
    trace: object
    input_hash: str
    publish: object

    def check(self):
        self.runner.shared_budget.check()
        row = self.runner.store.get(self.runner.job_id)
        if row and row["status"] in {"cancelled", "interrupted"}:
            self.runner.shared_budget.cancel("job cancelled or interrupted")
            self.runner.shared_budget.check()

    def remaining(self):
        return self.runner.shared_budget.remaining_seconds()

    def source_revision(self, source):
        return digest({"input_hash": self.input_hash, "source": source})

    def candidate_revision(self, source_revision, draft):
        return digest({"source_revision": source_revision, "draft": draft})


def input_identity(runner):
    images = []
    for image in runner.request["images"]:
        path = (runner.directory / image["file"]).resolve()
        if not path.is_relative_to(runner.directory.resolve()):
            raise ValueError("input path outside job")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != image["sha256"]:
            raise ValueError("original image changed after submission")
        images.append({"id": image["id"], "role": image["role"], "sha256": actual})
    return digest({"images": images, "text": runner.request["text"]})
