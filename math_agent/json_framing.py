"""Narrow outer-framing recovery for completed final JSON objects.

Keep the existing append-only closure, plus removal of exactly one surplus root
'}' after an otherwise complete object. Never repair strings/values/keys/commas,
internal mismatches, multiple documents or transport/token truncation. Callers
must have a completed/stop final response and must still run every stage gate.
"""
import hashlib
import json
import math
import re


def _finite_json_constant(value):
    raise ValueError('non-finite JSON constant: ' + value)


def _finite_json_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError('overflowing JSON number')
    return value


def trim_completed_root_suffix(text):
    """Return the exact first-object prefix only for a single redundant '}'.

    No heuristic search, regex parsing of JSON, evaluation or reserialization.
    Duplicate keys/non-finite constants are rejected at all nesting depths.
    A valid object followed by a second document or explanatory text is rejected.
    """
    from .compact_output import unique_object
    if not isinstance(text, str) or len(text) > 200000:
        return None
    start = len(text) - len(text.lstrip(' \t\r\n'))
    if start >= len(text) or text[start] != '{':
        return None
    decoder = json.JSONDecoder(object_pairs_hook=unique_object,
                               parse_constant=_finite_json_constant, parse_float=_finite_json_float)
    try:
        value, end = decoder.raw_decode(text, start)
    except (ValueError, RecursionError):
        return None
    if not isinstance(value, dict) or not re.fullmatch(r'[ \t\r\n]*}[ \t\r\n]*', text[end:]):
        return None
    return text[:end]


def close_completed_containers(text):
    trimmed = trim_completed_root_suffix(text)
    if trimmed is not None:
        return trimmed
    from .compact_output import unique_object
    if not isinstance(text,str) or not text.rstrip().endswith(('}',']')):return None
    try:json.loads(text,object_pairs_hook=unique_object)
    except json.JSONDecodeError as exc:
        if exc.pos<len(text.rstrip()):return None
    except ValueError:return None
    else:return None
    stack=[];quoted=False;escaped=False
    for char in text:
        if quoted:
            if escaped:escaped=False
            elif char=='\\':escaped=True
            elif char=='"':quoted=False
        elif char=='"':quoted=True
        elif char in '{[':stack.append(char)
        elif char in '}]':
            if not stack or stack.pop()!=('{' if char=='}' else '['):return None
    if quoted or escaped or not stack:return None
    repaired=text+''.join('}' if c=='{' else ']' for c in reversed(stack))
    try:value=json.loads(repaired,object_pairs_hook=unique_object)
    except ValueError:return None
    return repaired if isinstance(value,dict) else None


def audit_closure(audit,stage,call,original,repaired,transport):
    if original.startswith(repaired) and len(repaired) < len(original):
        audit('json_surplus_root_closure', {'stage':stage, 'call':call, 'transport':transport,
            'removed_suffix':original[len(repaired):],
            'before_sha256':hashlib.sha256(original.encode()).hexdigest(),
            'after_sha256':hashlib.sha256(repaired.encode()).hexdigest(),
            'detail':'完成态单一JSON对象后仅去除一个多余根闭合符；完整对象逐字保留，拒绝重复键/非有限数/第二对象，仍须全部阶段与发布校验'})
        return
    audit('json_container_closure',{'stage':stage,'call':call,'transport':transport,
        'suffix':repaired[len(original):],
        'before_sha256':hashlib.sha256(original.encode()).hexdigest(),
        'after_sha256':hashlib.sha256(repaired.encode()).hexdigest(),
        'detail':'API完成态仅追加缺少的外层闭合符号；原始文本逐字保留，不补字段/值，不重发请求，仍须全部阶段校验'})
