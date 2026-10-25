"""Observation: behavior-preserving services; runtime dependencies are explicit arguments."""
from __future__ import annotations
import copy
import json
from math_agent.rendering import RenderError
from math_agent.storage import write_json
from math_agent.scene_tools import compact_observation

def observe(job, candidate):
    job.observation_count += 1
    job.scenes.current = copy.deepcopy(candidate.spec)
    output = job.directory / 'output'
    if output.exists():
        for name in ['export-check.json', 'observation-failure.json']:
            (output / name).unlink(missing_ok=True)
        for path in output.glob('tab-*.png'):
            path.unlink()
    from math_agent.explanation_coverage import require as require_explanation
    require_explanation(candidate)
    try:
        job.renderer(candidate.spec, output)
    except RenderError:
        failed = output / 'observation-failure.json'
        if failed.exists():
            job.audit('observation', {'status': 'fail', 'evidence': json.loads(failed.read_text())})
        raise
    report_path = output / 'export-check.json'
    if not report_path.is_file():
        raise RenderError('缺少真实浏览器观察报告，不能进入复核')
    report = json.loads(report_path.read_text())
    if report.get('status') != 'pass':
        raise RenderError('浏览器观察未通过')
    observation = compact_observation(report, candidate.spec)
    write_json(job.directory / f'observation-{job.observation_count:02d}.json', observation)
    job.audit('observation', observation)
    return observation
