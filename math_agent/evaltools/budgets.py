"""Resolve provider output capacity without generating tokens or leaking credentials.

DeepSeek GET /models documents max_output_tokens. Unknown gateways must supply
an explicit --max-output-tokens; never silently fall back to a small cap.
"""
from __future__ import annotations
import httpx
from .inputs import EvaluationError


def resolve_output_limit(settings, override: int | None = None, *, transport=None) -> int:
    if override is not None:
        if type(override) is not int or override < 512:
            raise EvaluationError('--max-output-tokens 必须是至少512的整数。')
        return override
    if settings.api_style != 'deepseek':
        raise EvaluationError('此协议没有已确认的模型元数据入口，请用 --max-output-tokens 显式填写供应商输出上限。')
    try:
        with httpx.Client(timeout=httpx.Timeout(15, connect=5), transport=transport,
                          follow_redirects=False) as client:
            response = client.get(settings.base_url.rstrip('/') + '/models',
                headers={'Authorization': 'Bearer ' + settings.api_key})
        if response.status_code != 200:
            raise EvaluationError(f'读取模型上限失败（HTTP {response.status_code}），未发送生成请求；检查配置，或用 --max-output-tokens 指定输出上限。')
        data = response.json()
        models = data.get('data') if isinstance(data, dict) else None
        matches = [row for row in models if isinstance(row, dict) and row.get('id') == settings.model] if isinstance(models, list) else []
        if len(matches) != 1:
            raise ValueError()
        limit = matches[0].get('max_output_tokens')
        context = matches[0].get('context_window')
        if type(limit) is not int or limit < 512:
            raise ValueError()
        if context is not None and (type(context) is not int or context <= 0 or limit > context):
            raise ValueError()
        return limit
    except EvaluationError:
        raise
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        # Do not echo HTTP bodies, URLs with auth details or third-party errors.
        raise EvaluationError('无法读取当前模型的 max_output_tokens；未发送生成请求，也未回退低额度。可用 --max-output-tokens 指定供应商上限。') from None
