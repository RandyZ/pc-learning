"""Token-limit regressions are synthetic audit data; no paid/model calls."""
import copy
import json
import subprocess

import pytest

from benchmarks import suite
from benchmarks.diagnostics import budget_messages, diagnose


def truncated(reasoning=12000, cap=12000):
    return {'status':'failed','error':'模型输出被截断；没有把不完整答案交给网页引擎',
            'audit':[{'kind':'model_budget','stage':'source','call':1,'max_tokens':8000},
                     {'kind':'model','stage':'source','call':1,'finish_reason':'stop'},
                     {'kind':'model_budget','stage':'solve','call':2,'max_tokens':cap},
                     {'kind':'model','stage':'solve','call':2,'finish_reason':'length',
                      'usage':{'completion_tokens':12000,
                               'completion_tokens_details':{'reasoning_tokens':reasoning}}}]}


@pytest.mark.parametrize('reasoning,other', [(12000,0),(9263,2737)])
def test_reasoning_shares_output_budget(reasoning,other):
    value=diagnose(truncated(reasoning))
    assert value['code']=='MODEL_OUTPUT_TRUNCATED' and value['stage']=='solve'
    assert value['max_tokens']==12000 and value['output_tokens']==12000
    assert value['reasoning_tokens']==reasoning and value['non_reasoning_output_tokens']==other
    assert value['output_cap_reached'] is True
    assert '不自动重试' in value['suggestion']


def test_budget_comes_from_matching_request_not_total_or_source():
    record=truncated()
    record['audit'].insert(2,{'kind':'model_budget','stage':'solve','call':99,'max_tokens':64000})
    assert diagnose(record)['max_tokens']==12000


def test_context_length_remains_possible_without_output_cap_evidence():
    record=truncated(cap=32000)
    result=diagnose(record)
    assert not result['output_cap_reached']
    assert '不足以确认' in result['suggestion']
    record['audit']=[x for x in record['audit'] if x['kind']!='model_budget']
    assert diagnose(record)['max_tokens'] is None


def test_missing_or_inconsistent_usage_not_invented():
    record=truncated(15000)
    assert diagnose(record)['non_reasoning_output_tokens'] is None
    del record['audit'][-1]['usage']
    result=diagnose(record)
    assert result['output_tokens'] is None and result['reasoning_tokens'] is None
    assert not result['output_cap_reached']


def test_completed_or_later_error_not_attributed_to_old_truncation():
    record=truncated()
    record['status']='completed'
    assert diagnose(record)=={}
    record['status']='failed';record['error']='later browser error'
    assert diagnose(record)['code']=='TASK_FAILED'


def test_summary_does_not_echo_unknown_error_or_bad_stage():
    record={'status':'failed','error':'Authorization: sensitive-value',
            'audit':[{'kind':'model','stage':'<script>private</script>','finish_reason':'stop'}]}
    output=json.dumps(diagnose(record))
    assert 'sensitive-value' not in output and '<script>' not in output


def test_warning_prints_effective_budget_but_does_not_change_config():
    configs=[{'profile':'A1','thinking':True,'effort':'high','max_tokens':12000,
              'solve_max_tokens':0,'compact_tools':False},
             {'profile':'B0','thinking':True,'effort':'high','max_tokens':12000,
              'solve_max_tokens':32000,'compact_tools':True}]
    before=copy.deepcopy(configs);messages=budget_messages(configs)
    assert configs==before
    assert sum(m.startswith('警告') for m in messages)==1
    assert 'B0: solve额度=32000' in '\n'.join(messages)


def test_reading_old_run_enriches_report_without_loading_env_or_model(tmp_path,monkeypatch):
    monkeypatch.setattr(suite,'launch',lambda *a:pytest.fail('no replay'))
    monkeypatch.setattr(suite,'load_env',lambda *a:pytest.fail('no credentials needed'))
    runs=[]
    for profile,reasoning in [('A1',12000),('B0',9263)]:
        directory=tmp_path/'01'/profile
        job='a'*32
        jobdir=directory/'runtime/jobs'/job
        jobdir.mkdir(parents=True)
        (directory/'result.json').write_text(json.dumps({'case_id':'01','profile':profile,
            'job_id':job,'status':'failed','end_to_end_seconds':82,'model_calls':3,
            'effective_config':{'compact_tools':profile=='B0'}}))
        (jobdir/'report.json').write_text(json.dumps(truncated(reasoning)))
        runs.append({'case_id':'01','profile':profile,'launcher_status':'finished'})
    (tmp_path/'suite.json').write_text(json.dumps({'profiles':['A1','B0'],'runs':runs}))
    assert suite.main(['--report',str(tmp_path)])==0
    result=json.loads((tmp_path/'summary.json').read_text())
    assert [r['failure']['reasoning_tokens'] for r in result['runs']]==[12000,9263]
    assert result['groups']['A1']['completed']==0
    assert any('compact_tools' in warning for warning in result['warnings'])
    html=(tmp_path/'summary.html').read_text()
    assert '失败原因' in html and '12,000' in html and '9,263' in html


def test_external_task_report_never_read(tmp_path):
    root=tmp_path/'run';directory=root/'01/A1';jobdir=directory/'runtime/jobs'/('a'*32)
    jobdir.mkdir(parents=True)
    external=tmp_path/'private.json';external.write_text(json.dumps(truncated()))
    (jobdir/'report.json').symlink_to(external)
    (directory/'result.json').write_text(json.dumps({'case_id':'01','profile':'A1','job_id':'a'*32,'status':'failed'}))
    (root/'suite.json').write_text(json.dumps({'profiles':['A1'],'runs':[
        {'case_id':'01','profile':'A1','launcher_status':'finished'}]}))
    row=suite.report(root)['runs'][0]
    assert row['failure']=={} and '任务报告' not in row['links']


def test_preflight_config_warnings_are_printed_without_rewriting_environment(monkeypatch,capsys):
    configs=[{'profile':'B0','thinking':True,'effort':'high','max_tokens':12000,'solve_max_tokens':0}]
    env={'DEEPSEEK_MAX_TOKENS':'12000'};seen=[]
    def run(*args,**kwargs):
        seen.append(kwargs['env'].copy())
        return subprocess.CompletedProcess(args,0,'PREFLIGHT_CONFIG='+json.dumps(configs)+'\nPREFLIGHT_OK\n')
    monkeypatch.setattr(suite.subprocess,'run',run)
    suite.preflight(env,{'profiles':['B0'],'runs':[]})
    assert env==seen[0]=={'DEEPSEEK_MAX_TOKENS':'12000'}
    assert '警告' in capsys.readouterr().out
