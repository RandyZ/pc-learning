"""Freeze non-secret settings; never dump the environment or credential fields."""
from dataclasses import fields
import hashlib
from pathlib import Path
import subprocess
from ..config import ROOT


def effective(settings):
    excluded = {"api_key", "access_token", "data_dir", "redis_url", "ocr_url", "_inactive_configured_limits"}
    data = {f.name: getattr(settings, f.name) for f in fields(settings) if f.name not in excluded}
    data.update(redis_enabled=bool(settings.redis_url), ocr_enabled=bool(settings.ocr_url))
    if getattr(settings, "diagnostic_mode", False):
        inactive = ("job_timeout", "max_tokens", "source_max_tokens", "solve_max_tokens", "review_max_tokens")
        configured = getattr(settings, "_inactive_configured_limits", {})
        data["inactive_configured_limits"] = {key: configured.get(key, data[key]) for key in inactive}
        data.update({key: None for key in inactive})
        data.update(test_mode="diagnostic", output_limit_policy="provider_default",
                    sla_target_seconds=180, review_reserve_seconds=0)
    return data


def code_identity():
    from ..domain.v2 import digest
    package = ROOT / "math_agent"
    files = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(package.rglob("*.py"))}
    for p in sorted((package / "jsxgraph").rglob("*")):
        if p.is_file() and p.suffix in {".js", ".html", ".css", ".json"}:
            files[str(p.relative_to(ROOT))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return digest(files)
