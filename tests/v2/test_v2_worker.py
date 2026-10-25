"""Actual API -> original Worker -> V2 graph -> Redis/browser; only HTTP model output is mocked."""
import io
import json
import os
import threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import pytest
from PIL import Image
from math_agent.api import create_app
from math_agent.config import Settings
from math_agent.storage import Store
from math_agent.worker import supervise


@pytest.mark.skipif(os.getenv('MATH_AGENT_BROWSER_TEST')!='1',reason='requires real browser and Redis')
def test_v2_worker_and_cached_api_result(tmp_path,monkeypatch,source_v2,draft_v2,review_v2):
    from test_e2e import private_redis
    responses=[source_v2,draft_v2,review_v2];requests=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            if len(requests)>len(responses):self.send_error(500,'unexpected additional model call');return
            raw=json.dumps({'model':'offline-fixture','choices':[{'message':{'role':'assistant',
                'content':json.dumps(responses[len(requests)-1],ensure_ascii=False)},'finish_reason':'stop'}],
                'usage':{'prompt_tokens':10,'completion_tokens':10}}).encode()
            self.send_response(200);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with private_redis() as redis_url:
            from benchmarks.profiles import overrides
            for k,v in {**overrides('B2'),'AGENT_DATA_DIR':str(tmp_path), 'REDIS_URL':redis_url,
                'DEEPSEEK_API_KEY':'offline-test-only','AGENT_ACCESS_TOKEN':'x'*40,
                'DEEPSEEK_BASE_URL':f'http://127.0.0.1:{server.server_port}'}.items():monkeypatch.setenv(k,v)
            cfg=Settings();api=create_app(cfg).test_client();store=Store(tmp_path)
            auth={'Authorization':'Bearer '+cfg.access_token}
            picture=io.BytesIO();Image.new('RGB',(64,64),'white').save(picture,format='PNG')
            raw=picture.getvalue()
            for cached in (False,True):
                submission=api.post('/api/jobs',headers=auth,data={'text':source_v2['problem_text'],
                    'question':(io.BytesIO(raw),'fixture.png')})
                assert submission.status_code==202
                job=submission.json['id'];assert store.claim()==job
                supervise(cfg,store,job,lambda:False,lambda:None)
                row=store.get(job)
                assert row['status']=='completed',row
                report=json.loads((store.path(job)/'report.json').read_text())
                assert report['cache_hit'] is cached
                assert report['workflow']=='evidence_v2'
                assert report['model_calls_this_job']==(0 if cached else 3)
                assert api.get(f'/api/jobs/{job}/artifacts/result.html',headers=auth).status_code==200
            assert len(requests)==3
    finally:
        server.shutdown();thread.join(5);server.server_close()
