"""Exercise the actual diagnostic child and OCR join without any paid endpoint."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from benchmarks import diagnostic_mode as mode
from math_agent.storage import Store, write_json


@pytest.mark.parametrize('inactive_value', [None, '65536', ''])
@pytest.mark.parametrize('workflow', ['classic', 'evidence_v2'])
def test_actual_diagnostic_child_keeps_policy_and_stops_on_uncertain_source(tmp_path,monkeypatch,workflow,inactive_value):
    seen=[]
    source={'title':'诊断夹具','problem_text':'长度不清楚','conditions':[
        {'id':'C1','text':'线段AB','origin':'question_text','image_ids':[]}],
        'questions':[{'id':'Q1','text':'求长度'}], 'reference_summary':'', 'uncertainties':['缺少长度']}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            seen.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            raw=json.dumps({'model':'offline-fixture','choices':[{'message':{
                'role':'assistant','content':json.dumps(source,ensure_ascii=False)},'finish_reason':'stop'}],
                'usage':{'prompt_tokens':10,'completion_tokens':10}}).encode()
            self.send_response(200);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        for key,val in {'AGENT_DATA_DIR':str(tmp_path),'AGENT_WORKFLOW':workflow,'AGENT_COMPACT_TOOLS':'true',
                        'DEEPSEEK_API_KEY':'offline-test-key','AGENT_ACCESS_TOKEN':'x'*40,
                        'DEEPSEEK_BASE_URL':f'http://127.0.0.1:{server.server_port}',
                        'REDIS_URL':'','AGENT_OCR_URL':'','LANGSMITH_TRACING':'false'}.items():
            monkeypatch.setenv(key,val)
        if inactive_value is not None:
            for variable, _ in mode.INACTIVE_LIMITS.values():
                monkeypatch.setenv(variable, inactive_value)
            monkeypatch.setenv('AGENT_REVIEW_RESERVE_SECONDS', inactive_value)
        settings=mode.create_settings()
        store=Store(tmp_path);job,directory=store.prepare()
        write_json(directory/'request.json',{'text':'线段AB长度不明','images':[],'force':True})
        write_json(directory/'diagnostic-permission.json',{'job_id':job,'allow_paid':True,'mode':'diagnostic'})
        store.enqueue(job);store.claim()
        start=time.monotonic()
        # Test-only fail-safe, not a diagnostic execution deadline.
        mode.supervise(settings,store,job,lambda:time.monotonic()-start>20,lambda:None)
        row=store.get(job)
        assert row['status']=='needs_input',row
        assert len(seen)==1 and 'max_tokens' not in seen[0]
        budget=json.loads((directory/'shared-budget.json').read_text())
        assert budget['remaining_seconds'] is None
        assert (directory/'diagnostic-attempt.lock').exists()
        with pytest.raises(ValueError,match='freshly claimed'):
            mode.run_job(job)
        assert len(seen)==1
    finally:
        server.shutdown();thread.join(5);server.server_close()


def test_ocr_overlap_has_no_hidden_business_deadline_but_preserves_normal_join(monkeypatch):
    import math_agent.ocr_overlap as overlap
    waits=[]
    class Future:
        def result(self,timeout):waits.append(timeout);return []
    class Pool:
        def __init__(self,**kw):pass
        def submit(self,*a):return Future()
        def shutdown(self,**kw):pass
    monkeypatch.setattr(overlap,'ThreadPoolExecutor',Pool)
    for diagnostic in (False,True):
        pending=overlap.PendingOCR(SimpleNamespace(diagnostic_mode=diagnostic),{},None,
            lambda *a:None,time.monotonic()-100,lambda *a:[])
        assert pending.result()==[]
        pending.close()
    assert waits==[0,None]
