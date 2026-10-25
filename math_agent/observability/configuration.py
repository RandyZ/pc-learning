"""Local configuration manifest without credentials; never changes runtime settings."""
from dataclasses import fields
from pathlib import Path


def effective(settings):
    hidden = {"api_key", "access_token"}
    result = {}
    for field in fields(settings):
        if field.name in hidden:
            continue
        value = getattr(settings, field.name)
        result[field.name] = str(value) if isinstance(value, Path) else value
    result["key_configured"] = bool(settings.api_key)
    from math_agent.execution_limits import snapshot
    result["execution_limits"] = snapshot(settings)
    return result
