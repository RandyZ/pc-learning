"""Configuration regressions only: no API credentials, browser or model requests."""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from benchmarks.diagnostic_mode import INACTIVE_LIMITS, create_settings
from math_agent.config import Settings, ROOT
from math_agent.observability.effective_config import effective


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for key in list(os.environ):
        if key.startswith(('AGENT_', 'DEEPSEEK_', 'LANGSMITH_', 'LANGCHAIN_')) or key == 'REDIS_URL':
            monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize('field', list(INACTIVE_LIMITS))
@pytest.mark.parametrize('raw', ['0', '-1', '65536', '131072', '', 'none', 'unlimited'])
def test_inactive_environment_limits_cannot_block_diagnostic(tmp_path, monkeypatch, field, raw):
    variable, placeholder = INACTIVE_LIMITS[field]
    monkeypatch.setenv(variable, raw)
    before = dict(os.environ)
    settings = create_settings(data_dir=tmp_path)
    report = effective(settings)
    assert settings.diagnostic_mode and report[field] is None
    assert getattr(settings, field) == placeholder  # Compatibility value, never a wire/job cap.
    recorded = report['inactive_configured_limits'][field]
    assert recorded == int(raw) if raw.lstrip('-').isdigit() else isinstance(recorded, str)
    assert report['output_limit_policy'] == 'provider_default'
    assert settings.max_calls == 10 and settings.max_repairs == 2
    assert settings.http_timeout == 1200
    assert dict(os.environ) == before
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize('field', list(INACTIVE_LIMITS))
def test_explicit_inactive_override_is_recorded_not_enforced(tmp_path, field):
    settings = create_settings(data_dir=tmp_path, **{field: 131072})
    assert effective(settings)['inactive_configured_limits'][field] == 131072
    assert effective(settings)[field] is None


def test_all_inactive_limits_and_reserve_may_be_empty(tmp_path, monkeypatch):
    for variable, _ in INACTIVE_LIMITS.values():
        monkeypatch.setenv(variable, '')
    monkeypatch.setenv('AGENT_REVIEW_RESERVE_SECONDS', '')
    settings = create_settings(data_dir=tmp_path)
    assert settings.review_reserve_seconds == 0
    assert all(effective(settings)[key] is None for key in INACTIVE_LIMITS)


def test_actual_token_boundary_regression(tmp_path, monkeypatch):
    monkeypatch.setenv('DEEPSEEK_MAX_TOKENS', '65536')
    with pytest.raises(ValueError, match='DEEPSEEK_MAX_TOKENS.*65536'):
        Settings(data_dir=tmp_path)
    settings = create_settings(data_dir=tmp_path)
    assert effective(settings)['max_tokens'] is None
    assert effective(settings)['inactive_configured_limits']['max_tokens'] == 65536


@pytest.mark.parametrize('key,value', [
    ('AGENT_MAX_REPAIRS', '5'), ('AGENT_MAX_REPAIRS', '-1'), ('AGENT_MAX_REPAIRS', ''),
    ('AGENT_MAX_REPAIRS', 'unlimited'), ('AGENT_MAX_MODEL_CALLS', '0'),
    ('AGENT_MAX_MODEL_CALLS', '31'), ('AGENT_MAX_MODEL_CALLS', ''),
    ('AGENT_OCR_TIMEOUT', '0'), ('AGENT_OCR_TIMEOUT', '601'),
])
def test_active_protections_still_fail_and_identify_field(tmp_path, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    # An unrelated inactive cap must neither hide the real error nor disable safety.
    monkeypatch.setenv('DEEPSEEK_MAX_TOKENS', '65536')
    with pytest.raises(ValueError, match=key):
        create_settings(data_dir=tmp_path)


@pytest.mark.parametrize('field,key,value', [
    ('max_tokens', 'DEEPSEEK_MAX_TOKENS', 0),
    ('max_tokens', 'DEEPSEEK_MAX_TOKENS', 65536),
    ('source_max_tokens', 'DEEPSEEK_SOURCE_MAX_TOKENS', 65536),
    ('solve_max_tokens', 'DEEPSEEK_SOLVE_MAX_TOKENS', 65536),
    ('review_max_tokens', 'DEEPSEEK_REVIEW_MAX_TOKENS', 65536),
    ('job_timeout', 'AGENT_JOB_TIMEOUT', 0),
    ('job_timeout', 'AGENT_JOB_TIMEOUT', 3601),
    ('http_timeout', 'DEEPSEEK_HTTP_TIMEOUT', 1201),
])
def test_production_ranges_remain_unchanged(tmp_path, field, key, value):
    with pytest.raises(ValueError, match=key):
        Settings(data_dir=tmp_path, **{field: value})


def test_ignored_text_does_not_leak_into_audit(tmp_path, monkeypatch):
    secret = 'private-credential-do-not-log'
    monkeypatch.setenv('DEEPSEEK_MAX_TOKENS', secret)
    monkeypatch.setenv('DEEPSEEK_API_KEY', secret)
    settings = create_settings(data_dir=tmp_path)
    output = json.dumps(effective(settings), allow_nan=False)
    assert secret not in output
    assert '_inactive_configured_limits' not in output
    assert effective(settings)['inactive_configured_limits']['max_tokens'].startswith('[')
    monkeypatch.setenv('AGENT_MAX_REPAIRS', secret)
    with pytest.raises(ValueError) as exc:
        create_settings(data_dir=tmp_path)
    assert 'AGENT_MAX_REPAIRS' in str(exc.value) and secret not in str(exc.value)


def test_replace_preserves_inactive_snapshot_for_transport(tmp_path):
    settings = create_settings(data_dir=tmp_path, max_tokens=65536, job_timeout=0)
    clone = replace(settings, max_tokens=settings.source_max_tokens)
    assert clone.diagnostic_mode
    assert effective(clone)['inactive_configured_limits']['max_tokens'] == 65536
    assert effective(clone)['inactive_configured_limits']['job_timeout'] == 0
    assert effective(clone)['max_tokens'] is None


def test_normal_defaults_and_environment_cannot_enable_diagnostic(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_DIAGNOSTIC_MODE', 'true')
    normal = Settings(data_dir=tmp_path)
    assert normal.max_tokens == 12000 and normal.job_timeout == 240
    assert not getattr(normal, 'diagnostic_mode', False)
    assert '_inactive_configured_limits' not in effective(normal)


def test_fresh_python_child_sees_same_inactive_policy(tmp_path, monkeypatch):
    for key in ('DEEPSEEK_MAX_TOKENS', 'DEEPSEEK_SOURCE_MAX_TOKENS', 'DEEPSEEK_SOLVE_MAX_TOKENS'):
        monkeypatch.setenv(key, '65536')
    monkeypatch.setenv('DEEPSEEK_REVIEW_MAX_TOKENS', '')
    monkeypatch.setenv('AGENT_JOB_TIMEOUT', '0')
    monkeypatch.setenv('AGENT_DATA_DIR', str(tmp_path))
    script = ('import json; from benchmarks.diagnostic_mode import create_settings; '
              'from math_agent.observability.effective_config import effective; '
              'print(json.dumps(effective(create_settings()), allow_nan=False))')
    proc = subprocess.run([sys.executable, '-c', script], cwd=ROOT, capture_output=True,
                          text=True, timeout=10, env=dict(os.environ))
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report['max_tokens'] is None and report['job_timeout'] is None
    assert report['inactive_configured_limits']['max_tokens'] == 65536
    assert report['max_calls'] == 10 and report['max_repairs'] == 2


def test_custom_live_protections_are_not_silently_reset(tmp_path):
    settings = create_settings(data_dir=tmp_path, max_calls=7, max_repairs=3, ocr_timeout=80,
                               thinking=True, effort='high')
    assert (settings.max_calls, settings.max_repairs, settings.ocr_timeout) == (7, 3, 80)
    assert settings.thinking is True and settings.effort == 'high'
    with pytest.raises(ValueError):
        create_settings(data_dir=tmp_path, review_reserve_seconds=30)
