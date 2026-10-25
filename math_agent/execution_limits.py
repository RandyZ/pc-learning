"""Explicit validation policy; no production defaults are changed.

None denotes no application cap. Protocol, schema, tool sandbox and cancellation
checks remain active. Validation is not a claim of meeting the production SLA.
"""
import sys


def validation(settings):
    return getattr(settings, "validation_mode", False) is True


def task_timeout(settings):
    if validation(settings):
        return settings.validation_timeout or None
    return settings.job_timeout


def deadline(settings, started):
    seconds = task_timeout(settings)
    return None if seconds is None else started + seconds


def call_limit(settings):
    return None if validation(settings) else settings.max_calls


def repair_allowed(settings, count):
    return validation(settings) or count < settings.max_repairs


def graph_limit(settings, production_limit):
    # LangGraph requires an integer. This is an engine sentinel, not a business
    # budget; validation relies on explicit cancellation/optional task timeout.
    return sys.maxsize if validation(settings) else production_limit


def snapshot(settings):
    enabled = validation(settings)
    return {"mode": "validation" if enabled else "production",
            "task_timeout_seconds": task_timeout(settings),
            "model_call_limit": call_limit(settings),
            "repair_limit": None if enabled else settings.max_repairs,
            "tool_round_limit": None if enabled else 4,
            "model_output_tokens": (settings.validation_max_tokens or "auto") if enabled else settings.max_tokens,
            "stage_output_limits_overridden": enabled,
            "model_http_read_timeout_seconds": None if enabled else settings.http_timeout,
            "automatic_transport_retries": False,
            "tool_sandbox_limits_retained": True}
