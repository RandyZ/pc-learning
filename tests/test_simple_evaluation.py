"""No external model calls: fixture discovery, Worker boundary, reports and privacy."""
import csv
import json
import os
from pathlib import Path
import signal
import sys

from PIL import Image
import pytest

from math_agent.config import ROOT
from math_agent.evaltools import runner
from math_agent.evaltools.cli import main
from math_agent.evaltools.inputs import EvaluationError, discover, load_env, select
from math_agent.evaltools.reporting import compare, render_report, summary
from math_agent.storage import write_json


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for key in list(os.environ):
        if key.startswith(('DEEPSEEK_', 'AGENT_', 'LANGSMITH_', 'LANGCHAIN_')) or key == 'REDIS_URL':
            monkeypatch.delenv(key, raising=False)
    # All env additions made by the runner are restored after each test.
    original = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(original)


@pytest.fixture
def fixtures(tmp_path):
    directory = tmp_path/'items'
    directory.mkdir()
    for name in ('01.png', '01答案.png', '02.png', '02答案-1.png', '02答案-2.png', '10.png'):
        Image.new('RGB', (16, 16), 'white').save(directory/name)
    return directory


@pytest.fixture
def env_file(tmp_path):
    path = tmp_path/'.env'
    path.write_text('DEEPSEEK_API_KEY="private-test-secret"\nDEEPSEEK_REASONING_EFFORT=high\n'
                    'DEEPSEEK_MAX_TOKENS=32000\nAGENT_ACCESS_TOKEN="private-access-secret"\n'
                    'AGENT_DATA_DIR=/do-not-use-production\nREDIS_URL=redis://production:6379/0\n')
    return path


def test_discovery_and_natural_selection(fixtures):
    cases = discover(fixtures)
    assert list(cases) == [1, 2, 10]
    assert [p.name for p in cases[2].answers] == ['02答案-1.png', '02答案-2.png']
    assert [c.number for c in select('01-2,10,1', cases)] == [1, 2, 10]
    assert select('all', cases) == list(cases.values())
    assert not cases[10].answers


@pytest.mark.parametrize('expression', ['0', '2-1', '1,3', '1-', 'all,1', '100000000000', ''])
def test_bad_selection(expression, fixtures):
    with pytest.raises(EvaluationError):
        select(expression, discover(fixtures))


@pytest.mark.parametrize('extra', ['1.jpg', '03答案.png', 'not-a-number.png'])
def test_ambiguous_inventory(extra, fixtures):
    Image.new('RGB', (2, 2)).save(fixtures/extra)
    with pytest.raises(EvaluationError):
        discover(fixtures)


def test_dotenv_safe_quotes_bom_and_environment_precedence(tmp_path, monkeypatch):
    path = tmp_path/'.env'
    path.write_text('\ufeffexport DEEPSEEK_MODEL="中文 model" # comment\nDEEPSEEK_API_KEY="a=b#c"\n'
                    'DEEPSEEK_THINKING=false\nPATH=/untrusted\n', encoding='utf-8')
    monkeypatch.setenv('DEEPSEEK_MODEL', 'inherited')
    old_path = os.getenv('PATH')
    assert load_env(path) == ['DEEPSEEK_MODEL']
    assert os.getenv('DEEPSEEK_API_KEY') == 'a=b#c'
    assert os.getenv('DEEPSEEK_MODEL') == 'inherited'
    assert os.getenv('PATH') == old_path


@pytest.mark.parametrize('value', ['$(touch /tmp/not-run)', '${TOKEN}', '`id`', '"unterminated'])
def test_dotenv_never_executes_or_discloses(value, tmp_path):
    path = tmp_path/'.env'
    path.write_text('DEEPSEEK_API_KEY='+value)
    with pytest.raises(EvaluationError) as caught:
        load_env(path)
    assert value not in str(caught.value)
    assert 'DEEPSEEK_API_KEY' not in os.environ


def test_duplicate_env_is_rejected(tmp_path):
    path = tmp_path/'.env'
    path.write_text('DEEPSEEK_MODEL=a\nDEEPSEEK_MODEL=b')
    with pytest.raises(EvaluationError):
        load_env(path)


def test_dry_run_has_no_model_browser_or_ocr_calls(fixtures, env_file, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'preflight', lambda _: pytest.fail('preflight on dry run'))
    monkeypatch.setattr(runner, 'original_supervisor', lambda *_: pytest.fail('model on dry run'))
    out, code = runner.run_batch(select('1-2', discover(fixtures)), ROOT, env_file=env_file,
                                 out=tmp_path/'dry', dry_run=True)
    assert code == 0
    rows = json.loads((out/'runs.json').read_text())
    assert [r['status'] for r in rows] == ['prepared', 'prepared']
    assert json.loads((out/'summary.json').read_text())['attempted'] == 0
    manifest = (out/'manifest.json').read_text()
    assert 'private-test-secret' not in manifest and 'private-access-secret' not in manifest
    assert '/do-not-use-production' not in manifest
    assert json.loads(manifest)['settings']['max_tokens'] == 32000
    for row in rows:
        request = json.loads((out/'runtime/jobs'/row['job_id']/'request.json').read_text())
        assert request['force'] is True
        assert request['images'][0]['role'] == 'question'
        assert all(i['role'] == 'answer' for i in request['images'][1:])
    assert (out/'report.html').is_file()


def fake_supervisor(cfg, store, job, stop, heartbeat):
    assert str(cfg.data_dir) == os.environ['AGENT_DATA_DIR']
    assert cfg.redis_url == ''
    assert cfg.effort == 'high' and cfg.max_tokens == 32000
    assert os.environ['LANGSMITH_TRACING'] == 'false'
    store.event(job, 'solve');heartbeat()
    directory = store.path(job)
    output = directory/'output';output.mkdir()
    (output/'result.html').write_text('<html>offline fixture</html>')
    write_json(directory/'report.json', {'model_calls_this_job':2})
    write_json(directory/'audit.json', [
        {'kind':'model', 'stage':'source', 'call':1, 'seconds':20, 'duration_seconds':5,
         'usage':{'completion_tokens':10}},
        {'kind':'model', 'stage':'solve', 'call':2, 'seconds':50, 'duration_seconds':7,
         'usage':{'output_tokens':20, 'output_tokens_details':{'reasoning_tokens':8}}}])
    store.finish(job, 'completed')


def test_batch_results_and_human_quality(fixtures, env_file, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'preflight', lambda _: None)
    monkeypatch.setattr(runner, 'original_supervisor', fake_supervisor)
    out, code = runner.run_batch(select('1-2', discover(fixtures)), ROOT, env_file=env_file, out=tmp_path/'paid')
    assert code == 0
    rows = json.loads((out/'runs.json').read_text())
    assert rows[0]['known_output_tokens'] == 30  # Reasoning is already included.
    assert rows[0]['perf']['model_wall_seconds_known'] == 12  # Not cumulative 20+50.
    assert rows[0]['usage_complete'] is True
    assert rows[0]['model_calls'] == 2
    assert (out/rows[0]['artifact']).is_file()
    value = json.loads((out/'summary.json').read_text())
    assert value['auto_completed'] == 2 and value['human_confirmed_correct'] == 0
    (out/'quality.csv').write_text('case_id,math,proof,diagram,interaction,coverage,notes\n'
                                  '01,true,true,true,true,true,checked\n'
                                  '02,true,true,false,true,true,diagram issue\n')
    value = render_report(out)
    assert value['human_confirmed_correct'] == 1
    assert value['confirmed_correct_rate'] == .5
    assert value['confirmed_correct_within_180s_rate'] == .5
    with pytest.raises(EvaluationError):
        runner.run_batch(select('1', discover(fixtures)), ROOT, out=out, dry_run=True)


def test_failure_continues_without_replay_and_hides_key(fixtures, env_file, tmp_path, monkeypatch):
    calls = []
    def fail_then_pass(cfg, store, job, stop, heartbeat):
        calls.append(job)
        if len(calls) == 1:
            store.event(job, 'review');heartbeat()
            store.finish(job, 'failed', error='private-test-secret <script>alert(1)</script>')
        else:
            fake_supervisor(cfg, store, job, stop, heartbeat)
    monkeypatch.setattr(runner, 'preflight', lambda _: None)
    monkeypatch.setattr(runner, 'original_supervisor', fail_then_pass)
    out, code = runner.run_batch(select('1-2', discover(fixtures)), ROOT, env_file=env_file, out=tmp_path/'batch')
    assert code == 1 and len(set(calls)) == 2
    rows = json.loads((out/'runs.json').read_text())
    assert rows[0]['status'] == 'failed' and rows[0]['model_calls'] is None
    assert rows[0]['failure_stage'] == 'review'
    assert rows[0]['artifact'] is None and not rows[0]['usage_complete']
    assert 'private-test-secret' not in (out/'runs.json').read_text()
    page = (out/'report.html').read_text()
    assert '<script>alert(1)</script>' not in page
    assert '&lt;script&gt;' in page
    assert json.loads((out/'summary.json').read_text())['failed_or_incomplete'] == 1


def test_cancel_stops_new_admissions(fixtures, env_file, tmp_path, monkeypatch):
    def interrupt(cfg, store, job, stop, heartbeat):
        os.kill(os.getpid(), signal.SIGINT)
        assert stop()
        store.finish(job, 'interrupted', error='test cancellation')
    monkeypatch.setattr(runner, 'preflight', lambda _: None)
    monkeypatch.setattr(runner, 'original_supervisor', interrupt)
    before = signal.getsignal(signal.SIGINT)
    out, code = runner.run_batch(select('1-2', discover(fixtures)), ROOT, env_file=env_file, out=tmp_path/'stop')
    assert code == 130 and signal.getsignal(signal.SIGINT) == before
    value = json.loads((out/'summary.json').read_text())
    assert value['selected'] == 2 and value['attempted'] == 1 and value['not_started'] == 1


def test_auth_failure_stops_rest(fixtures, env_file, tmp_path, monkeypatch):
    def denied(cfg, store, job, stop, heartbeat):
        write_json(store.path(job)/'audit.json', [{'kind':'model', 'stage':'source','status':401}])
        store.finish(job, 'failed', error='unauthorized')
    monkeypatch.setattr(runner, 'preflight', lambda _: None)
    monkeypatch.setattr(runner, 'original_supervisor', denied)
    out, code = runner.run_batch(select('1-2', discover(fixtures)), ROOT, env_file=env_file, out=tmp_path/'auth')
    assert code == 1
    rows = json.loads((out/'runs.json').read_text())
    assert rows[1]['status'] == 'not_started'


def test_invalid_later_image_prevents_entire_paid_batch(fixtures, env_file, tmp_path, monkeypatch):
    (fixtures/'02.png').write_bytes(b'not an image')
    monkeypatch.setattr(runner, 'preflight', lambda _: None)
    monkeypatch.setattr(runner, 'original_supervisor', lambda *_: pytest.fail('paid before all inputs validated'))
    with pytest.raises(ValueError):
        runner.run_batch(select('1-2', discover(fixtures)), ROOT, env_file=env_file, out=tmp_path/'invalid')


def test_real_worker_supervisor_without_model(fixtures, env_file, tmp_path, monkeypatch):
    from math_agent.worker import supervise
    child = tmp_path/'finish_offline.py'
    child.write_text("import os,sys\nfrom pathlib import Path\nfrom math_agent.storage import Store,write_json\n"
                     "s=Store(Path(os.environ['AGENT_DATA_DIR']));j=sys.argv[1];d=s.path(j)\n"
                     "(d/'output').mkdir();(d/'output/result.html').write_text('<html>offline</html>')\n"
                     "write_json(d/'report.json',{'model_calls_this_job':0});s.finish(j,'completed')\n")
    # Child imports this source checkout, exactly as original Worker cwd semantics.
    monkeypatch.setenv('PYTHONPATH', str(ROOT))
    def actual(cfg, store, job, stop, heartbeat):
        return supervise(cfg, store, job, stop, heartbeat, command=[sys.executable, str(child), job])
    monkeypatch.setattr(runner, 'preflight', lambda _: None)
    monkeypatch.setattr(runner, 'original_supervisor', actual)
    out, code = runner.run_batch(select('1', discover(fixtures)), ROOT, env_file=env_file, out=tmp_path/'worker')
    assert code == 0
    rows = json.loads((out/'runs.json').read_text())
    assert rows[0]['model_calls'] == 0


def test_cli_list_never_loads_dotenv(fixtures, capsys):
    assert main(['list', '--fixtures', str(fixtures)]) == 0
    assert '共 3 题' in capsys.readouterr().out
    assert 'DEEPSEEK_API_KEY' not in os.environ


def test_compare_warns_on_changed_input_and_model(fixtures, env_file, tmp_path, monkeypatch):
    cases = select('1', discover(fixtures))
    a, _ = runner.run_batch(cases, ROOT, env_file=env_file, out=tmp_path/'a', dry_run=True)
    b, _ = runner.run_batch(cases, ROOT, env_file=env_file, out=tmp_path/'b', dry_run=True)
    manifest = json.loads((b/'manifest.json').read_text());manifest['settings']['model'] = 'different'
    write_json(b/'manifest.json', manifest)
    rows = json.loads((b/'runs.json').read_text());rows[0]['input_hash'] = 'different'
    write_json(b/'runs.json', rows)
    result = compare(a, b)
    assert any('model' in x for x in result['warnings'])
    assert any('输入' in x for x in result['warnings'])
    assert result['pairs'][0]['right_minus_left_seconds'] is None


def test_bad_quality_is_not_silently_accepted(fixtures, env_file, tmp_path):
    out, _ = runner.run_batch(select('1', discover(fixtures)), ROOT, env_file=env_file, out=tmp_path/'quality', dry_run=True)
    (out/'quality.csv').write_text('case_id,math,proof,diagram,interaction,coverage\n01,maybe,,,,\n')
    with pytest.raises(EvaluationError):
        render_report(out)


def test_submitted_repository_corpus():
    directory = ROOT/'tests/test-items'
    if not directory.is_dir():
        pytest.skip('source-only local workspace; full submitted corpus checked in CI')
    cases = discover(directory)
    assert cases and all(case.answers for case in cases.values())
    for case in cases.values():
        for path in (case.question, *case.answers):
            with Image.open(path) as image:
                image.verify()
