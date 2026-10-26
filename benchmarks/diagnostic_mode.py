"""Explicit benchmark-only policy. No production environment switch or retries.

No job/phase deadline; no client-specified output limit (provider defaults still
apply). Network inactivity, tool limits, calls, repair limits and cancellation
remain bounded. Only benchmarks.run may authorize one fresh diagnostic task.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import re
import subprocess
import sys
import time

from math_agent.config import Settings, ROOT


# These values only keep inherited numeric fields usable by existing adapters.
# Diagnostic transport omits all output caps and its supervisor has no deadline.
# Never parse inactive .env settings through Settings' integer default factories.
INACTIVE_LIMITS = {
    "job_timeout": ("AGENT_JOB_TIMEOUT", 240),
    "max_tokens": ("DEEPSEEK_MAX_TOKENS", 12000),
    "source_max_tokens": ("DEEPSEEK_SOURCE_MAX_TOKENS", 8000),
    "solve_max_tokens": ("DEEPSEEK_SOLVE_MAX_TOKENS", 0),
    "review_max_tokens": ("DEEPSEEK_REVIEW_MAX_TOKENS", 0),
}


def _inactive_value(value):
    """Record numeric policy inputs, never arbitrary environment text or secrets."""
    if value is None:
        return None
    if type(value) is int and -(2**63) <= value < 2**63:
        return value
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"[+-]?[0-9]{1,18}", text):
            return int(text)
        if not text:
            return "[empty; ignored]"
    return "[not a supported integer; ignored]"


@dataclass(frozen=True)
class DiagnosticSettings(Settings):
    # Not read from .env: normal API/Worker/CLI cannot accidentally enable it.
    diagnostic_mode: bool = field(default=True, init=False)
    _inactive_configured_limits: dict = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self):
        super().__post_init__()
        if self.api_style != "deepseek":
            raise ValueError("Diagnostic output-limit omission supports DeepSeek only")
        if self.review_reserve_seconds:
            raise ValueError("Diagnostic mode cannot reserve a phase deadline")
        if self.redis_url:
            raise ValueError("Diagnostic runs must disable the application cache")


def create_settings(**overrides) -> DiagnosticSettings:
    # Normalize only inactive policies BEFORE inherited factories and validation.
    # Valid protections (calls, repairs, OCR and network) remain fully validated.
    values = dict(overrides)
    recorded = {}
    for name, (variable, placeholder) in INACTIVE_LIMITS.items():
        configured = values.pop(name) if name in values else os.getenv(variable, placeholder)
        recorded[name] = _inactive_value(configured)
        values[name] = placeholder
    values["_inactive_configured_limits"] = recorded
    return DiagnosticSettings(**{"review_reserve_seconds": 0, "http_timeout": 1200,
                                 "redis_url": "", **values})


def supervise(settings, store, job_id, stop, heartbeat, command=None):
    """Original process isolation/cleanup, deliberately without the job timer.

    Do not alter the production Worker or pass Infinity as a fake timeout. The
    child still uses the same Runner/graphs and writes the same task artifacts.
    """
    from math_agent.worker import terminate
    if not getattr(settings, "diagnostic_mode", False):
        raise ValueError("unbounded supervisor requires explicit diagnostic settings")
    if stop():
        store.finish(job_id, "interrupted", error="诊断任务已取消；未发出模型请求")
        return
    command = command or [sys.executable, "-m", "benchmarks.diagnostic_mode", job_id]
    process = subprocess.Popen(command, start_new_session=True, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, cwd=ROOT,
        env={**os.environ, "AGENT_DATA_DIR": str(settings.data_dir.resolve())})
    try:
        while process.poll() is None:
            heartbeat()
            row = store.get(job_id)
            if stop():
                store.finish(job_id, "interrupted", error="诊断任务被手动停止；已发请求可能计费，未自动重试")
                break
            if row["status"] == "cancelled":
                break
            time.sleep(.4)
    finally:
        terminate(process)
    if store.get(job_id)["status"] == "running":
        store.finish(job_id, "failed", error="诊断进程异常退出；没有自动重新调用模型")


def run_job(job_id: str) -> None:
    """One-shot entry; --prepare-only and previously used tasks cannot bill again."""
    from math_agent.storage import Store
    settings = create_settings()
    store = Store(settings.data_dir)
    directory = store.path(job_id)
    permission = json.loads((directory / "diagnostic-permission.json").read_text())
    if permission != {"job_id": job_id, "allow_paid": True, "mode": "diagnostic"}:
        raise ValueError("missing explicit diagnostic authorization")
    if store.get(job_id)["status"] != "running":
        raise ValueError("diagnostic task must be freshly claimed")
    if any((directory / name).exists() for name in ("audit.json", "report.json", "runtime-manifest.json")):
        raise ValueError("no automatic diagnostic resume/replay")
    with (directory / "diagnostic-attempt.lock").open("x"):
        pass
    from math_agent.pipeline import Runner
    Runner(settings, job_id).run()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Use benchmarks.run --diagnostic --allow-paid, not this internal entrypoint")
    run_job(sys.argv[1])
