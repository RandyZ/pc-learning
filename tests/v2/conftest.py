import copy
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from math_agent.config import Settings
from math_agent.storage import Store, write_json
from math_agent.pipeline import Runner
from math_agent.provider import DeepSeek
from math_agent.domain.v2 import Draft
from math_agent.contracts import Source


@pytest.fixture
def source_v2():
    return {"title":"中点", "problem_text":"线段AB长度为2，M是中点，求AM。",
            "conditions":[{"id":"C1","text":"AB=2，M是中点","origin":"question_text","image_ids":[]}],
            "questions":[{"id":"Q1","text":"求AM"}], "reference_summary":"", "uncertainties":[]}


@pytest.fixture
def draft_v2():
    return {"schema_version":"evidence-v2.1", "questions":{"Q1":{
        "proof":{"result":"AM=1", "steps":[{"id":"half", "premise":"AB=2，M是中点", "rule":"中点定义",
            "conclusion":"AM=AB/2=1", "basis_ids":["C1"]}], "cases":[], "domain_and_boundaries":"AB>0",
            "condition_ids":["C1"]},
        "computations":[{"id":"half_length","op":"simplify", "target_step_id":"half", "basis_ids":["C1"], "expression":"2/2"}],
        "scene":{"objects":[{"id":"A","kind":"point","coords":[0,0],"label":"A"},
            {"id":"B","kind":"point","coords":[2,0],"label":"B"},
            {"id":"M","kind":"midpoint","parents":["A","B"],"label":"M"},
            {"id":"AB","kind":"segment","parents":["A","B"]}],
            "required_visible":["A","B","M"],"step_links":{"half":["M"]}}}}}


@pytest.fixture
def review_v2():
    return {"verdict":"pass", "conditions":{"C1":{"verdict":"pass","evidence":"题设一致"}},
            "questions":{"Q1":{"verdict":"pass","evidence":"中点及长度均核对"}},"issues":[],
            "operation_reviews":{"half_length":{"verdict":"pass","method":"receipt","basis_ids":["C1"],"evidence":"2/2=1，适用中点定义"}}}


class MemoryCache:
    def __init__(self, settings, audit):
        self.data = {}
    def get(self, key):
        return copy.deepcopy(self.data.get(key))
    def put(self, key, value):
        self.data[key] = copy.deepcopy(value)


def fake_renderer(spec, directory):
    directory.mkdir(parents=True, exist_ok=True)
    write_json(directory/'spec.json', spec)
    (directory/'result.html').write_text('<html>offline fixture only</html>')
    # A real PNG is needed when independent review attaches trusted screenshots.
    from PIL import Image
    Image.new('RGB',(64,64),'white').save(directory/'preview.png')
    write_json(directory/'export-check.json', {'status':'pass','engine':'JSXGraph','samples':[], 'sample_count':0})


@pytest.fixture
def make_runner(tmp_path):
    runners = []
    def make(replies, *, profile='serial', render=fake_renderer, cache=None, **settings):
        cfg = Settings(data_dir=tmp_path/f'run-{len(runners)}', api_key='offline-test-key', access_token='a'*40,
            workflow='evidence_v2',compact_tools=True, v2_profile=profile, max_calls=12, **settings)
        store = Store(cfg.data_dir)
        job_id, directory = store.prepare()
        write_json(directory/'request.json',{'text':'线段AB长度为2，M是中点，求AM。', 'images':[], 'force':False})
        store.enqueue(job_id); store.claim()
        sent=[]
        def handler(request):
            body = json.loads(request.content)
            sent.append(body)
            if len(sent)>len(replies):
                raise AssertionError('unexpected extra paid-equivalent call: '+str(len(sent)))
            reply = replies[len(sent)-1]
            if callable(reply): reply = reply(body)
            if request.url.path.endswith('/responses'):
                return httpx.Response(200,json={"model":"offline-test-model","status":"completed",
                    "output":[{"type":"message","content":[{"type":"output_text","text":json.dumps(reply,ensure_ascii=False)}]}],
                    "usage":{"input_tokens":10,"output_tokens":10}})
            return httpx.Response(200,json={"model":"offline-test-model","choices":[{"message":{
                "role":"assistant","content":json.dumps(reply,ensure_ascii=False)},"finish_reason":"stop"}],
                "usage":{"prompt_tokens":10,"completion_tokens":10,"completion_tokens_details":{"reasoning_tokens":4}}})
        runner=Runner(cfg,job_id,lambda s,a:DeepSeek(s,a,transport=httpx.MockTransport(handler)),
                      (lambda s,a:cache) if cache is not None else MemoryCache,render)
        runner.sent=sent
        runners.append(runner)
        return runner
    yield make
    for r in runners:
        r.provider.close()
