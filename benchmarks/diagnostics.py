"""Offline, allowlisted diagnostics. Never retry, grow budgets, or parse partial answers."""
from __future__ import annotations

import re


def count(value):
    return value if type(value) is int and value >= 0 else None


def label(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]{0,63}', value) else None


# Summary metadata is a whitelist, not raw exception or model text.
FIELD_PATH = re.compile(r'(?:questions|updates)\.Q[1-9][0-9]*\.(?:proof|computations|scene)(?:\.(?:[A-Za-z_][A-Za-z0-9_]{0,39}|[0-9]{1,3})){0,8}')


def field_paths(event):
    paths = []
    for item in event.get('errors', []) if isinstance(event.get('errors'), list) else []:
        path = item.get('path') if isinstance(item, dict) else None
        if isinstance(path, str) and FIELD_PATH.fullmatch(path):
            paths.append(path)
    # Historical runs predate structured paths. Match only entire path lines,
    # never input values, mathematical content, credentials or stack traces.
    detail = event.get('detail', '')
    if not paths and isinstance(detail, str):
        paths = [line.strip() for line in detail.splitlines() if FIELD_PATH.fullmatch(line.strip())]
    return list(dict.fromkeys(paths))[:16]


def output_failure(audit, last, error):
    if not last:
        return None
    # Ignore already-handled errors from prior requests and unrelated stages.
    index = max(i for i, event in enumerate(audit) if event is last)
    following = audit[index+1:]
    stage = label(last.get('stage')) or 'unknown'
    if 'JSON' in error or 'json' in error:
        events = [e for e in following if e.get('kind') == 'model_output_error'
                  and e.get('call') == last.get('call') and e.get('stage') == last.get('stage')]
        if events:
            event = events[-1]
            position = count(event.get('position'))
            where = f'（字符位置 {position}，从0计数）' if position is not None else ''
            return {'code': 'MODEL_JSON_INVALID', 'stage': stage, 'call': count(last.get('call')),
                    'position': position,
                    'message': f'{stage} 阶段返回的最终 JSON 无效{where}；不是业务超时。',
                    'suggestion': '查看本地 report.json 中的 model_output_error；原输出已保留，不自动重试。'}
    contracts = [e for e in following if e.get('kind') == 'contract_error'
                 and e.get('stage') == last.get('stage')]
    detail = contracts[-1].get('detail') if contracts else None
    if contracts and isinstance(detail, str) and detail and (not error or error == detail
                      or error.startswith(detail[:120])):
        event = contracts[-1]
        paths = field_paths(event)
        where = '：' + '、'.join(paths) if paths else ''
        return {'code': 'MODEL_CONTRACT_INVALID', 'stage': stage, 'call': count(last.get('call')),
                'paths': paths, 'message': f'{stage} 阶段输出未通过契约或字段修订校验{where}。',
                'suggestion': '查看本地 contract_error 和原模型输出；不会删除检查项或自动增加调用预算。'}
    return None


def diagnose(report: dict) -> dict:
    """Use the failed request's own budget and usage, not cumulative token totals.

    Keep arbitrary exception/response text out of summary reports. The original
    private report remains linked for errors this small classifier cannot explain.
    A completed task never inherits a previously handled truncation as a failure.
    """
    if report.get('status') == 'completed':
        return {}
    audit = report.get('audit', [])
    audit = [item for item in audit if isinstance(item, dict)] if isinstance(audit, list) else []
    models = [item for item in audit if item.get('kind') == 'model' and not item.get('not_sent')]
    last = models[-1] if models else {}
    error = report.get('error', '')
    error = error if isinstance(error, str) else ''
    stage_events = [item for item in audit if label(item.get('stage'))]
    stage = label(last.get('stage')) if last.get('finish_reason') == 'length' else None
    stage = stage or (label(stage_events[-1].get('stage')) if stage_events else None)
    diagnostic = {'code': 'TASK_FAILED', 'stage': stage,
                  'message': '任务未完成；详细原因请查看本地任务报告。'}
    if last.get('finish_reason') != 'length':
        specific = output_failure(audit, last, error)
        if specific:
            return specific
        if '总时限' in error or '任务超时' in error:
            diagnostic.update(code='JOB_DEADLINE_EXCEEDED', message='任务达到总时限；已发出的请求可能仍计费，不自动重试。')
        return diagnostic
    # A later non-truncation error is not attributed to an older model event.
    if error and not any(word in error.lower() for word in ('截断', 'truncat', 'max_tokens', 'length')):
        return diagnostic
    usage = last.get('usage') or {}
    usage = usage if isinstance(usage, dict) else {}
    details = usage.get('completion_tokens_details') or usage.get('output_tokens_details') or {}
    details = details if isinstance(details, dict) else {}
    output = count(usage.get('completion_tokens', usage.get('output_tokens')))
    reasoning = count(details.get('reasoning_tokens'))
    non_reasoning = output - reasoning if output is not None and reasoning is not None and reasoning <= output else None
    call = count(last.get('call'))
    budgets = [item for item in audit if item.get('kind') == 'model_budget'
               and call is not None and count(item.get('call')) == call and item.get('stage') == last.get('stage')]
    cap = count(budgets[-1].get('max_tokens')) if budgets else None
    hit_cap = cap is not None and cap > 0 and output is not None and output >= cap
    message = f"{stage or '模型'} 阶段输出被截断（finish_reason=length）"
    if cap is not None:
        message += f'；单次额度 {cap:,}'
    if output is not None:
        message += f'，已生成 {output:,}'
    if reasoning is not None:
        message += f'，其中思考 {reasoning:,}'
    if non_reasoning is not None:
        message += f'、非思考输出 {non_reasoning:,}'
    message += '。'
    diagnostic.update(code='MODEL_OUTPUT_TRUNCATED', stage=stage, call=call, finish_reason='length',
                      max_tokens=cap, output_tokens=output, reasoning_tokens=reasoning,
                      non_reasoning_output_tokens=non_reasoning, output_cap_reached=hit_cap,
                      message=message,
                      suggestion=('思考计入输出额度。可显式提高两组相同的 Token 上限后另开测试；'
                                  '提高额度只避免过早截断，不保证更快、正确或成功，也可能增加费用。'
                                  if hit_cap else
                                  '需区分输出额度与上下文长度限制；当前日志不足以确认哪一项耗尽。')
                      + '不采用截断 JSON，不自动重试。')
    return diagnostic


def budget_messages(configurations: list[dict]) -> list[str]:
    """Informational only. 12k is an observed risk threshold, not an API minimum."""
    messages = []
    for cfg in configurations:
        profile = label(cfg.get('profile')) or 'unknown'
        cap = count(cfg.get('solve_max_tokens')) or count(cfg.get('max_tokens'))
        effort = label(cfg.get('effort')) or 'unknown'
        compact = cfg.get('compact_tools')
        messages.append(f"{profile}: solve额度={cap if cap is not None else '未知'}，"
                        f"thinking={cfg.get('thinking') is True}，effort={effort}，compact={compact is True}")
        if cfg.get('thinking') is True and effort in {'high', 'max'} and cap is not None and 0 < cap <= 12000:
            messages.append(f'警告：{profile} 使用 {effort} 思考但解题额度仅 {cap:,}，思考与最终输出共用额度。'
                            '此组合在本题实测中发生过截断；这不是接口最低额度要求。脚本不会自动改配置或重试。')
    return messages
