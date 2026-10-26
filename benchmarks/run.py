"""Explicit single fresh-task evaluation through the unchanged Worker supervisor.

No automatic repeat, no paid call without --allow-paid, no production data paths.
Read credentials from the environment; never pass a key on the command line.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time
from types import SimpleNamespace

from .profiles import PROFILES, overrides
from math_agent.config import Settings, ROOT
from math_agent.image_input import save_image
from math_agent.storage import Store, write_json
from math_agent.worker import supervise
from math_agent.observability.effective_config import effective, code_identity
from math_agent.observability.timing import summarize
from math_agent.domain.v2 import digest


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile',choices=PROFILES,required=True)
    parser.add_argument('--case-id',required=True)
    parser.add_argument('--question',type=Path,action='append',default=[])
    parser.add_argument('--answer',type=Path,action='append',default=[])
    parser.add_argument('--text',default='')
    parser.add_argument('--out',type=Path,required=True,help='New isolated directory, must not already exist')
    parser.add_argument('--prepare-only',action='store_true',help='Prepare a fresh local task without any model call')
    parser.add_argument('--allow-paid',action='store_true',help='Explicit authorization for one bounded new task')
    parser.add_argument('--diagnostic',action='store_true',
        help='No whole-job deadline or client output cap; provider and safety limits remain')
    args=parser.parse_args(argv)
    if not args.prepare_only and not args.allow_paid:
        parser.error('no model request is made without --allow-paid; use --prepare-only for offline setup')
    if not args.text.strip() and not args.question:
        parser.error('provide original question text or image')
    if len(args.question)+len(args.answer)>6:
        parser.error('at most six images')
    if not args.prepare_only and not os.getenv('DEEPSEEK_API_KEY'):
        parser.error('set DEEPSEEK_API_KEY in the environment; do not put it in arguments or files')
    out=args.out.resolve()
    out.mkdir(parents=True,exist_ok=False,mode=0o700)
    os.environ.update(overrides(args.profile))
    os.environ.update(AGENT_DATA_DIR=str(out/'runtime'),REDIS_URL='')
    if args.diagnostic:
        from .diagnostic_mode import create_settings
        cfg=create_settings()
    else:
        cfg=Settings()
    store=Store(cfg.data_dir)
    job,directory=store.prepare()
    images=[]
    for i,(role,path) in enumerate([('question',p) for p in args.question]+[('answer',p) for p in args.answer],1):
        with path.open('rb') as stream:
            sha=save_image(SimpleNamespace(stream=stream),directory/f'I{i}.png')
        images.append({'id':f'I{i}','role':role,'file':f'I{i}.png','sha256':sha})
    write_json(directory/'request.json',{'text':args.text,'images':images,'force':True})
    config=effective(cfg)
    try:
        commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True,timeout=3).strip()
        dirty=bool(subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True,timeout=3).strip())
    except (OSError,subprocess.SubprocessError):commit=None;dirty=None
    manifest={'case_id':args.case_id,'profile':args.profile,'job_id':job,'commit':commit,'dirty':dirty,
        'code_hash':code_identity(),'effective_config':config,'end_to_end':True,'application_cache':False,
        'test_mode':'diagnostic' if args.diagnostic else 'standard','soft_target_seconds':180,
        'provider_prefix_cache':'record actual returned usage; not claimed disabled',
        'input_hash':digest({'text':args.text,'images':[{'role':i['role'],'sha256':i['sha256']} for i in images]})}
    write_json(out/'manifest.json',manifest)
    write_json(directory/'effective-config.json',config)
    store.enqueue(job)
    if args.prepare_only:
        print(json.dumps({'status':'prepared','job_id':job,'directory':str(out),'paid_requests':0}))
        return 0
    if args.diagnostic:
        write_json(directory/'diagnostic-permission.json', {'job_id':job,'allow_paid':True,'mode':'diagnostic'})
    accepted=store.get(job)['created'];store.claim();started=time.time()
    stopped=False
    def stop(*_):
        nonlocal stopped
        stopped=True
    signal.signal(signal.SIGINT,stop);signal.signal(signal.SIGTERM,stop)
    if args.diagnostic:
        from .diagnostic_mode import supervise as diagnostic_supervise
        diagnostic_supervise(cfg,store,job,lambda:stopped,lambda:None)
    else:
        supervise(cfg,store,job,lambda:stopped,lambda:None)
    row=store.get(job)
    report=json.loads((directory/'report.json').read_text()) if (directory/'report.json').exists() else {}
    audit=json.loads((directory/'audit.json').read_text()) if (directory/'audit.json').exists() else []
    # Artifact-ready time comes from completed publication, not polling overhead.
    ready=row['updated']
    result={**manifest,'status':row['status'],'queue_seconds':max(0,started-accepted),
        'execution_seconds':max(0,ready-started),'end_to_end_seconds':max(0,ready-accepted),
        'client_until_supervisor_return_seconds':time.time()-accepted,
        'model_calls':report.get('model_calls_this_job'), 'quality':None,
        'completed_within_180s':row['status']=='completed' and max(0,ready-accepted)<=180,
        'cost':None,'cost_complete':False,'error_type':None if row['status']=='completed' else 'TaskNotCompleted',
        'ocr_cache_hits':[x.get('cache_hit') for x in audit if x['kind']=='ocr' and x.get('status')=='done'],
        'perf':summarize({'status':row['status'],'seconds':max(0,ready-started),'audit':audit})}
    write_json(out/'result.json',result)
    print(json.dumps({'profile':args.profile,'status':row['status'],'job_id':job,
        'end_to_end_seconds':result['end_to_end_seconds'],'result':str(out/'result.json')},ensure_ascii=False))
    return 0 if row['status']=='completed' else 1


if __name__=='__main__':raise SystemExit(main())
