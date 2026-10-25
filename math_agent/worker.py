from __future__ import annotations
import fcntl
import os
import signal
import subprocess
import sys
import time
from .storage import Store, write_json
from .config import ROOT


def terminate(process):
    # The root may already have exited while a browser/helper remains in its group.
    try: os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError: pass
    if process.poll() is None:
        try: process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            process.wait()
    try: os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError: pass


def supervise(settings, store, job_id, stop, heartbeat, command=None):
    command = command or [sys.executable, "-m", "math_agent.cli", "run-job", job_id]
    process = subprocess.Popen(command, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        cwd=ROOT, env={**os.environ,"AGENT_DATA_DIR":str(settings.data_dir.resolve())})
    from .execution_limits import task_timeout
    limit = task_timeout(settings)
    start = time.monotonic()
    try:
        while process.poll() is None:
            heartbeat()
            row = store.get(job_id)
            if stop():
                store.finish(job_id, "interrupted", error="Worker 停止；未自动重新调用模型")
                break
            if row["status"] == "cancelled": break
            if limit is not None and time.monotonic() - start > limit:
                store.finish(job_id, "failed", error="任务超过总时限，计算进程已终止；已发出模型请求可能仍计费")
                break
            time.sleep(.4)
    finally:
        terminate(process)
    if store.get(job_id)["status"] == "running":
        store.finish(job_id, "failed", error="任务进程异常退出，请检查部署依赖；没有自动重新调用模型")


def run_worker(settings, once=False):
    store = Store(settings.data_dir)
    with (settings.data_dir / "worker.lock").open("w") as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise RuntimeError("同一数据目录只能运行一个 Worker") from None
        stopped = False
        def stop(*_):
            nonlocal stopped
            stopped = True
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        heartbeat = lambda: write_json(settings.data_dir / "worker-heartbeat.json", {"at": time.time(), "pid": os.getpid()})
        store.recover()
        try:
            while not stopped:
                heartbeat()
                job_id = store.claim()
                if job_id: supervise(settings, store, job_id, lambda: stopped, heartbeat)
                if once: break
                time.sleep(.5)
        finally:
            (settings.data_dir / "worker-heartbeat.json").unlink(missing_ok=True)
