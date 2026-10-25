"""Compare actual prompts, tools, routing and outputs with the fixed original Runner."""
import copy
from dataclasses import replace
import hashlib
import json
import os
import subprocess
import types
import pytest
from math_agent.config import ROOT
from math_agent.pipeline import Runner
from math_agent.storage import Store,write_json
from test_pipeline import MemoryCache,renderer

@pytest.fixture(scope='module')
def original():
    ref=os.getenv('REFACTOR_TEST_BASELINE','d23b43549e5a897a6e348da130b546c6f10b80db')
    try:
        raw=subprocess.check_output(['git','show',ref+':math_agent/pipeline.py'],cwd=ROOT,stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        pytest.fail('Fetch the fixed main baseline (git fetch origin main) for differential tests')
    # Reject a moving/new implementation as the baseline, even when a ref is overridden.
    expected=(Path(__file__).with_name('original-pipeline.sha256')).read_text().strip()
    assert hashlib.sha256(raw).hexdigest()==expected
    module=types.ModuleType('math_agent._original_pipeline')
    module.__package__='math_agent';module.__file__=str(ROOT/'math_agent/pipeline.py')
    exec(compile(raw,module.__file__,'exec'),module.__dict__)
    return module.Runner

from pathlib import Path


def run_case(cls,settings,sample,path,scenario,compact=False):
    cfg=replace(settings,data_dir=path,compact_tools=compact)
    store=Store(path);job,directory=store.prepare()
    write_json(directory/'request.json',{'text':'fixed offline task','images':[],'force':False})
    store.enqueue(job);store.claim()
    source,candidate,review=copy.deepcopy(sample)
    solve=candidate
    if compact:
        from math_agent.compact_output import pack
        from math_agent.contracts import Candidate
        solve=pack(Candidate.model_validate(candidate))
    # Compact review uses a different output contract; differential fixture below stays classic.
    queue=[source,solve,review]
    if scenario=='math':
        bad=copy.deepcopy(review);bad.update(verdict='revise',issues=[{'stage':'math','target':'Q2','detail':'boundary'}]);bad['questions'][1]['verdict']='issue'
        queue=[source,solve,bad,{'solutions':candidate['solutions']},review]
    elif scenario=='source':
        bad=copy.deepcopy(review);bad.update(verdict='revise',issues=[{'stage':'source','target':'C1','detail':'reread'}])
        queue=[source,solve,bad,source,solve,review]
    elif scenario=='unknown':
        source['uncertainties']=['unclear label'];queue=[source]
    elif scenario=='format':
        bad=copy.deepcopy(candidate);bad['solutions'][0]['condition_ids']=['missing']
        queue=[source,bad,candidate,review]
    elif scenario=='transport':queue=[]
    captures=[]
    class Provider:
        def __init__(self,*a):self.calls=0
        def generate(self,stage,messages,**kwargs):
            self.calls+=1
            captures.append({'stage':stage,'messages':copy.deepcopy(messages),'tools':copy.deepcopy(kwargs.get('tools')),
                             'schema':copy.deepcopy(kwargs.get('output_schema'))})
            if scenario=='transport':
                from math_agent.provider import ModelError
                raise ModelError('offline transport failure')
            return copy.deepcopy(queue.pop(0))
        def close(self):pass
    runner=cls(cfg,job,Provider,MemoryCache,renderer);runner.run()
    report=json.loads((directory/'report.json').read_text())
    # Job elapsed timestamps are carried in historical tool evidence only (none in fixture).
    return captures, {k:report.get(k) for k in ['status','error','source','review','model_calls_this_job']},[
        x['node'] for x in runner.records if x['kind']=='graph_node']


@pytest.mark.parametrize('scenario',['success','math','source','unknown','format','transport'])
def test_original_and_refactored_prompts_tools_outputs_identical(original,settings,sample,tmp_path,scenario):
    before=run_case(original,settings,sample,tmp_path/'before',scenario)
    after=run_case(Runner,settings,sample,tmp_path/'after',scenario)
    assert after==before
