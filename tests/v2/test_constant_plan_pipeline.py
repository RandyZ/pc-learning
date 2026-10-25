"""Real V2 graph/math/compiler with only model HTTP and optional renderer mocked."""
import json
import os

import pytest


@pytest.mark.parametrize('profile', ['serial', 'parallel_reuse'])
@pytest.mark.parametrize('kind', ['evaluate', 'simplify'])
def test_constant_assertion_reaches_independent_review_without_format_retry(
        make_runner, source_v2, draft_v2, review_v2, profile, kind):
    operation = draft_v2['questions']['Q1']['computations'][0]
    operation.update(op=kind, expected='1')
    runner = make_runner([source_v2, draft_v2, review_v2], profile=profile)
    runner.run()
    report = json.loads((runner.directory/'report.json').read_text())
    assert report['status'] == 'completed', report.get('error')
    assert report['model_calls_this_job'] == 3
    assert not any(event['kind'] == 'contract_error' for event in runner.records)
    bundle = json.loads((runner.directory/'v2-evidence.json').read_text())
    receipt = bundle['receipts']['half_length']
    assert receipt['verdict'] == 'pass' and receipt['result']['expected'] == '1'
    assert receipt['result']['exact'] == '1'


@pytest.mark.skipif(os.getenv('MATH_AGENT_BROWSER_TEST') != '1', reason='requires Chromium')
def test_constant_assertion_with_real_browser(make_runner, source_v2, draft_v2, review_v2):
    from math_agent.rendering import render
    draft_v2['questions']['Q1']['computations'][0].update(op='evaluate', expected='1')
    # Bind a real plotted coordinate to the calculated value, not equality residual=0.
    draft_v2['questions']['Q1']['scene']['objects'].append(
        {'id':'P', 'kind':'point', 'coords':['@half_length', 1], 'label':'P'})
    runner = make_runner([source_v2, draft_v2, review_v2], render=render)
    runner.run()
    report = json.loads((runner.directory/'report.json').read_text())
    assert report['status'] == 'completed', report.get('error')
    assert report['model_calls_this_job'] == 3
    assert report['render_check']['sample_count'] >= 1
    objects = report['render_check']['samples'][0]['objects']
    assert objects['P']['x'] == pytest.approx(1)
