"""Local reports distinguish automatic completion from human-verified correctness."""
from __future__ import annotations

import csv
import html
import json
from pathlib import Path
import statistics

from .inputs import EvaluationError
from ..storage import write_json

QUALITY = ('math', 'proof', 'diagram', 'interaction', 'coverage')
UNSTARTED = {'not_started', 'prepared'}


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, OSError):
        raise EvaluationError(f'{path.name} 无法读取；未把损坏记录当作成功或空结果。') from None


def quality_labels(out: Path, case_ids: set[str]) -> dict:
    path = out / 'quality.csv'
    if not path.exists():
        with path.open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=('case_id', *QUALITY, 'notes'))
            writer.writeheader()
            writer.writerows({'case_id': c} for c in sorted(case_ids))
    labels = {}
    with path.open(encoding='utf-8-sig', newline='') as stream:
        for raw in csv.DictReader(stream):
            cid = raw.get('case_id', '').strip()
            if cid not in case_ids or cid in labels:
                raise EvaluationError('quality.csv 有未知或重复题号；请保留原 case_id。')
            row = {}
            for key in QUALITY:
                value = (raw.get(key) or '').strip().lower()
                if value not in {'', 'true', 'false', '1', '0'}:
                    raise EvaluationError(f'quality.csv 的 {cid}/{key} 只能填 true、false 或留空。')
                row[key] = None if not value else value in {'true', '1'}
            labels[cid] = row
    return labels


def summary(rows: list[dict], labels: dict) -> dict:
    attempted = [r for r in rows if r['status'] not in UNSTARTED]
    completed = [r for r in attempted if r['status'] == 'completed']
    correct = [r for r in completed if all(labels.get(r['case_id'], {}).get(k) is True for k in QUALITY)]
    latency = [r['end_to_end_seconds'] for r in completed if isinstance(r.get('end_to_end_seconds'), (int, float))]
    fast = [r for r in correct if isinstance(r.get('end_to_end_seconds'), (int, float)) and r['end_to_end_seconds'] <= 180]
    return {
        'selected': len(rows), 'attempted': len(attempted),
        'not_started': len(rows)-len(attempted), 'auto_completed': len(completed),
        'failed_or_incomplete': len(attempted)-len(completed),
        'human_confirmed_correct': len(correct),
        'human_unreviewed': sum(any(labels.get(r['case_id'], {}).get(k) is None for k in QUALITY) for r in attempted),
        'auto_completion_rate': len(completed)/len(attempted) if attempted else None,
        'confirmed_correct_rate': len(correct)/len(attempted) if attempted else None,
        'confirmed_correct_within_180s_rate': len(fast)/len(attempted) if attempted else None,
        'completed_latency_median_seconds': statistics.median(latency) if latency else None,
        'completed_latency_range_seconds': [min(latency), max(latency)] if latency else None,
        'cost': None,
        'note': '自动 completed 不等于数学正确。人工未评分时正确率仅为已确认下界；失败/超时保留，未启动题单列。费用未知，不伪造报价。',
    }


def render_report(out: Path) -> dict:
    rows = read_json(out/'runs.json')
    if not isinstance(rows, list) or not rows:
        raise EvaluationError('指定目录没有 runs.json；请使用本脚本生成的评测目录。')
    labels = quality_labels(out, {r['case_id'] for r in rows})
    value = summary(rows, labels)
    manifest = read_json(out/'manifest.json', {})
    value['budget_mode'] = manifest.get('budget_mode', 'production')
    value['execution_limits'] = manifest.get('settings', {}).get('execution_limits')
    write_json(out/'summary.json', value)
    columns = ('case_id', 'status', 'end_to_end_seconds', 'execution_seconds', 'model_calls',
               'known_output_tokens', 'usage_complete', 'failure_stage', 'error', *QUALITY, 'artifact')
    with (out/'summary.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction='ignore')
        writer.writeheader()
        for row in rows:
            clean = {k: v for k, v in {**row, **labels.get(row['case_id'], {})}.items() if k in columns}
            # Neutralize spreadsheet formulas if an upstream error string begins with one.
            for k, v in clean.items():
                if isinstance(v, str) and v.lstrip().startswith(('=', '+', '-', '@')):
                    clean[k] = "'" + v
            writer.writerow(clean)
    table = []
    for row in rows:
        duration = row.get('end_to_end_seconds')
        seconds = f'{duration:.2f}' if isinstance(duration, (int, float)) else '—'
        artifact = row.get('artifact')
        link = ''
        if artifact:
            path = (out/artifact).resolve()
            if path.is_relative_to(out.resolve()) and path.is_file():
                link = f'<a href="{html.escape(artifact, quote=True)}" target="_blank" rel="noopener">打开讲解</a>'
        scores = labels.get(row['case_id'], {})
        verdict = '通过' if all(scores.get(k) is True for k in QUALITY) else '不通过' if any(scores.get(k) is False for k in QUALITY) else '待评'
        fields = (row['case_id'], row['status'], seconds, row.get('model_calls'), verdict, row.get('error', ''))
        table.append('<tr>' + ''.join(f'<td>{html.escape(str(x if x is not None else "—"))}</td>' for x in fields) + f'<td>{link}</td></tr>')
    page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>本地数学题评测</title>
<style>body{font:16px system-ui;max-width:1300px;margin:32px auto;padding:0 20px}table{border-collapse:collapse;width:100%}td,th{padding:10px;text-align:left;border:1px solid #bbb}td{max-width:400px;overflow-wrap:anywhere}pre{white-space:pre-wrap}</style>
<h1>本地数学题评测</h1><p>自动完成不代表正确。编辑 quality.csv 后重新执行 report 命令更新人工评分；报告不调用模型。</p>'''
    page += '<pre>' + html.escape(json.dumps(value, ensure_ascii=False, indent=2)) + '</pre>'
    page += '<table><tr><th>题号</th><th>状态</th><th>就绪耗时/秒</th><th>模型调用</th><th>人工评分</th><th>错误</th><th>产物</th></tr>' + ''.join(table) + '</table></html>'
    (out/'report.html').write_text(page, encoding='utf-8')
    return value


def compare(left: Path, right: Path) -> dict:
    a, b = read_json(left/'runs.json'), read_json(right/'runs.json')
    ma, mb = read_json(left/'manifest.json'), read_json(right/'manifest.json')
    if not all(isinstance(x, list) for x in (a, b)) or not all(isinstance(x, dict) for x in (ma, mb)):
        raise EvaluationError('compare 需要两个本脚本生成的评测目录。')
    warnings = []
    for key in sorted(set(ma['settings']) | set(mb['settings'])):
        if ma['settings'].get(key) != mb['settings'].get(key):
            warnings.append('配置不同：' + key)
    ia, ib = ({r['case_id']: r for r in group} for group in (a, b))
    if ia.keys() != ib.keys():
        warnings.append('两组选择的题号不同。')
    pairs = []
    for cid in sorted(ia.keys() & ib.keys()):
        x, y = ia[cid], ib[cid]
        if x.get('input_hash') != y.get('input_hash'):
            warnings.append(cid + '：实际输入或图片角色不同。')
        tx, ty = x.get('end_to_end_seconds'), y.get('end_to_end_seconds')
        pairs.append({'case_id': cid, 'left_status': x['status'], 'right_status': y['status'],
                      'left_seconds': tx, 'right_seconds': ty,
                      'right_minus_left_seconds': round(ty-tx, 3) if all(isinstance(t, (int,float)) for t in (tx,ty)) else None})
    return {'left': render_report(left), 'right': render_report(right), 'pairs': pairs,
            'warnings': warnings, 'note': '仅展示配对结果；失败更早结束不能算提速。证明/图形仍须人工评审。'}
