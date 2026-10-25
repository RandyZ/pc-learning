"""Summarize all submitted runs; never equate automatic pass with human correctness."""
from __future__ import annotations
import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path

QUALITY_FIELDS=('math','proof','diagram','interaction','coverage')
MODEL_SETTINGS=('model','base_url','api_style','thinking','effort','review_effort','diagram_thinking',
                'max_tokens','solve_max_tokens','review_max_tokens','source_max_tokens',
                'max_calls','max_repairs','job_timeout','ocr_profile','ocr_timeout')


def correct(row):
    quality=row.get('quality')
    return row.get('status')=='completed' and isinstance(quality,dict) and all(quality.get(k) is True for k in QUALITY_FIELDS)


def summarize(rows):
    groups=defaultdict(list)
    by_case=defaultdict(list)
    for row in rows:
        groups[row['profile']].append(row);by_case[row['case_id']].append(row)
    warnings=[]
    for case,items in by_case.items():
        if len({r.get('input_hash') for r in items})>1:warnings.append(case+': different input bytes')
        for key in MODEL_SETTINGS:
            if len({json.dumps(r.get('effective_config',{}).get(key),sort_keys=True) for r in items})>1:
                warnings.append(case+': unequal '+key)
        if len({bool(r.get('end_to_end')) for r in items})>1:warnings.append(case+': mixed replay and end-to-end scope')
    output={}
    for profile,items in groups.items():
        valid=[r for r in items if correct(r)]
        latency=[r['end_to_end_seconds'] for r in valid]
        completed=sum(r['status']=='completed' for r in items)
        costs=[r.get('cost') for r in items]
        cost_known=all(r.get('cost_complete') and isinstance(r.get('cost'),(float,int)) for r in items)
        output[profile]={'runs':len(items),'completed':completed,'failed_or_incomplete':len(items)-completed,
            'quality_unreviewed':sum(not isinstance(r.get('quality'),dict) or not all(type(r['quality'].get(k)) is bool for k in QUALITY_FIELDS) for r in items),
            'confirmed_correct':len(valid),'confirmed_correct_rate':len(valid)/len(items),
            'confirmed_correct_within_180s_rate':sum(r['end_to_end_seconds']<=180 for r in valid)/len(items),
            'correct_runs_latency_p50':statistics.median(latency) if latency else None,
            'correct_runs_latency_range':[min(latency),max(latency)] if latency else None,
            'average_known_model_calls':statistics.mean([r['model_calls'] for r in items if isinstance(r.get('model_calls'),int)])
                if any(isinstance(r.get('model_calls'),int) for r in items) else None,
            'cost_per_correct_including_failed_attempts':sum(costs)/len(valid) if cost_known and valid else None,
            'cost_complete':cost_known}
    return {'groups':output,'comparison_warnings':warnings,
        'note':'Unreviewed is not correct. Rates are confirmed lower bounds until human review completes; all failures remain in denominators. No small-sample P95 claim.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('results',type=Path,nargs='+');parser.add_argument('--quality',type=Path)
    parser.add_argument('--out',type=Path)
    args=parser.parse_args()
    labels=json.loads(args.quality.read_text()) if args.quality else {}
    rows=[]
    for path in args.results:
        row=json.loads(path.read_text())
        if row['job_id'] in labels:row['quality']=labels[row['job_id']]
        rows.append(row)
    text=json.dumps(summarize(rows),ensure_ascii=False,indent=2)
    if args.out:args.out.write_text(text,encoding='utf-8')
    print(text)


if __name__=='__main__':main()
