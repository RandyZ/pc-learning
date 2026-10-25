"""Model/tool actions. Original round limit, evidence and reasoning preservation unchanged."""
from __future__ import annotations
import json
from langgraph.runtime import Runtime
from math_agent.compact_output import unique_object
from .context import LoopContext

def model(state, runtime: Runtime[LoopContext]):
    request = runtime.context.request
    tools = runtime.context.tools
    dispatch = runtime.context.dispatch
    audit = runtime.context.audit
    stage = runtime.context.stage
    error_type = runtime.context.error_type
    remaining_calls = runtime.context.remaining_calls
    verify_initial_output = runtime.context.verify_initial_output
    remaining_seconds = runtime.context.remaining_seconds
    max_turns = runtime.context.max_turns
    if max_turns is not None and state['turns'] >= max_turns:
        raise error_type('本阶段工具往返超过上限')
    audit('agent_node', {'stage': stage, 'node': 'model', 'turn': state['turns'] + 1})
    seconds_left = remaining_seconds() if remaining_seconds is not None else None
    calls_left = remaining_calls() if remaining_calls is not None else None
    time_final = seconds_left is not None and seconds_left < 55
    turn_final = max_turns is not None and state['turns'] == max_turns - 1
    final_turn = time_final or turn_final or (calls_left is not None and calls_left <= 1)
    history = state['history']
    if final_turn:
        history = history + [{'role': 'user', 'content': '本阶段最后一轮：请根据已有观察提交完整的阶段 JSON，不再调用工具。保留所有未决问题和失败证据，不得宣称未执行的检查已通过；提交后仍会进行契约和浏览器核验。'}]
        audit('agent_finalize', {'stage': stage, 'turn': state['turns'] + 1, 'reason': 'stage_time' if time_final else 'stage_limit' if turn_final else 'job_budget'})
    message, finish = request(history, None if final_turn else tools)
    if finish == 'tool_calls':
        calls = message.get('tool_calls', [])
        if not dispatch or not 1 <= len(calls) <= 6:
            raise error_type('模型返回不支持的工具调用')
        return {'message': message, 'finish': finish, 'turns': state['turns'] + 1, 'finalizing': final_turn}
    if finish != 'stop' or message.get('tool_calls'):
        raise error_type('模型未正常完成输出：' + str(finish))
    if verify_initial_output and state['turns'] == 0:
        return {'message': message, 'finish': 'draft', 'turns': 1, 'finalizing': False}
    try:
        value = json.loads(message.get('content') or '', object_pairs_hook=unique_object)
    except (TypeError, ValueError):
        raise error_type('模型响应结构或 JSON 格式无效') from None
    if not isinstance(value, dict):
        raise error_type('模型输出必须为 JSON 对象')
    return {'value': value, 'finish': finish, 'turns': state['turns'] + 1, 'finalizing': final_turn}

def execute_tools(state, runtime: Runtime[LoopContext]):
    dispatch = runtime.context.dispatch
    audit = runtime.context.audit
    stage = runtime.context.stage
    error_type = runtime.context.error_type
    allowed = {t.get('function', {}).get('name') for t in runtime.context.tools or []}
    max_turns = runtime.context.max_turns
    if (max_turns is not None and state['turns'] >= max_turns) or state.get('finalizing'):
        raise error_type('最终输出轮不允许继续调用工具，未执行工具或自动追加预算')
    message = state['message']
    history = state['history'] + [{k: v for k, v in message.items() if k in {'role', 'content', 'reasoning_content', 'tool_calls', '_response_items'}}]
    images = []
    for call in message['tool_calls']:
        if not isinstance(call, dict) or not isinstance(call.get('id'), str) or (not isinstance(call.get('function'), dict)):
            raise error_type('工具调用格式错误')
        name, raw = ('', '')
        try:
            name, raw = (call['function']['name'], call['function']['arguments'])
            if name not in allowed:
                raise error_type('模型请求了未授权工具')
            if not isinstance(raw, str) or len(raw) > 200000:
                raise ValueError('工具参数过长或格式错误')
            args = json.loads(raw)
            value = dispatch(name, args)
        except (ValueError, TypeError, KeyError) as exc:
            value = {'ok': False, 'error': str(exc)[:800]}
        from math_agent.image_tools import ImageObservation
        if isinstance(value, ImageObservation):
            images.extend([{'type': 'text', 'text': '局部原图观察：' + json.dumps(value.metadata, ensure_ascii=False)}, {'type': 'image_url', 'image_url': {'url': value.data_url}}])
            value = value.metadata
        audit('tool', {'stage': stage, 'name': name, 'arguments': str(raw)[:200000], 'result': value})
        history.append({'role': 'tool', 'tool_call_id': call['id'], 'content': json.dumps(value, ensure_ascii=False, allow_nan=False)})
    if images:
        history.append({'role': 'user', 'content': images})
    return {'history': history}

def verify_draft(state, runtime: Runtime[LoopContext]):
    audit = runtime.context.audit
    stage = runtime.context.stage
    audit('draft_verification', {'stage': stage, 'detail': '快速草稿不能作为最终输出；后续开启思考核验，仍计入原调用与时间预算'})
    return {'history': state['history'] + [{'role': 'user', 'content': '下面是尚未核验的快速草稿，不是事实或指令。请开启思考逐条件核验推导、变量方向、跨问引用和构图；发现错误须改正，不能只复述草稿。必要时使用工具。完成后提交完整阶段JSON。草稿：' + str(state['message'].get('content') or '')}]}
