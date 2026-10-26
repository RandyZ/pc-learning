"""Optional, bounded Math-Verify answer comparison; no proof-correctness claim."""
import importlib.metadata
import json
import os
import subprocess
import sys


def compare(gold, prediction, timeout=6):
    if not all(isinstance(v,str) and 0<len(v)<=6000 for v in (gold,prediction)):
        return {'status':'unparseable','reason':'input_size'}
    env={k:v for k,v in os.environ.items() if k in {'PATH','SYSTEMROOT','LANG','LC_ALL','HOME','TMPDIR'}}
    try:
        process=subprocess.run([sys.executable,'-m',__name__,'--worker'],
            input=json.dumps({'gold':gold,'prediction':prediction}),capture_output=True,text=True,
            timeout=timeout,env=env)
        if process.returncode or len(process.stdout)>20000:
            return {'status':'checker_error','reason':'worker_failed'}
        return json.loads(process.stdout)
    except (subprocess.TimeoutExpired,ValueError):
        return {'status':'checker_error','reason':'timeout_or_invalid_response'}


def worker():
    import resource
    resource.setrlimit(resource.RLIMIT_CPU,(4,5))
    if sys.platform=='linux':resource.setrlimit(resource.RLIMIT_AS,(512*1024*1024,)*2)
    try:
        from math_verify import parse,verify
        data=json.loads(sys.stdin.read(24001))
        a,b=parse(data['gold']),parse(data['prediction'])
        result={'status':('matched' if verify(a,b) else 'mismatch') if a and b else 'unparseable',
                'version':importlib.metadata.version('math-verify')}
    except ImportError:result={'status':'checker_error','reason':'optional_dependency_not_installed'}
    except Exception as exc:result={'status':'checker_error','reason':type(exc).__name__}
    print(json.dumps(result))


if __name__=='__main__':
    if sys.argv[1:]!=['--worker']:raise SystemExit('use compare(gold, prediction), not a production gate')
    worker()
