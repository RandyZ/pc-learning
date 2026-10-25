"""Fresh, sequential jobs through the original Worker, never paid automatic retries."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
from types import SimpleNamespace
import uuid

from .inputs import Case, EvaluationError, load_env
from .reporting import read_json, render_report
from ..storage import Store, write_json

# Explicit allowlist: future credentials added to Settings cannot leak by accident.
PUBLIC_SETTINGS = (
    'model', 'base_url', 'api_style', 'review_responses', 'solve_responses',
    'overlap_ocr', 'native_constructions', 'incremental_math_review', 'incremental_review',
    'compact_tools', 'parallel_questions', 'parallel_review', 'question_concurrency',
    'solve_draft', 'review_plan', 'reference_mode', 'thinking', 'diagram_thinking',
    'effort', 'review_effort', 'max_tokens', 'solve_max_tokens', 'review_max_tokens',
    'review_reserve_seconds', 'source_max_tokens', 'ocr_profile', 'ocr_timeout',
    'job_timeout', 'max_calls', 'http_timeout', 'max_repairs',
)


def code_identity(root: Path) -> dict:
    digest = hashlib.sha256()
    paths = sorted((root/'math_agent').rglob('*.py')) + [root/'constraints.txt', root/'scripts/evaluate.py']
    for path in paths:
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    def git(*args):
        try:
            return subprocess.check_output(['git', *args], cwd=root, text=True, stderr=subprocess.DEVNULL, timeout=5).strip()
        except (OSError, subprocess.SubprocessError):
            return None
    dirty = git('status', '--porcelain', '--untracked-files=no')
    return {'commit': git('rev-parse', 'HEAD'), 'branch': git('branch', '--show-current'),
            'tracked_dirty': bool(dirty) if dirty is not None else None, 'code_sha256': digest.hexdigest()}


def redact(message: str) -> str:
    for name in ('DEEPSEEK_API_KEY', 'AGENT_ACCESS_TOKEN', 'LANGSMITH_API_KEY'):
        secret = os.getenv(name)
        if secret:
            message = message.replace(secret, '[REDACTED]')
    return message[:2000]


def preflight(cfg) -> None:
    """No model request. Check dependencies once before accepting a paid batch."""
    from playwright.sync_api import sync_playwright
    from ..ocr import healthy
    if not cfg.api_key:
        raise EvaluationError('未配置 DEEPSEEK_API_KEY；写入本地 .env 或环境变量，不要放入命令参数。')
    try:
        with sync_playwright() as playwright:
            executable = os.getenv('MATH_AGENT_CHROMIUM_EXECUTABLE') or os.getenv('GGB_CHROMIUM_EXECUTABLE')
            browser = playwright.chromium.launch(headless=True, executable_path=executable)
            browser.close()
    except Exception:
        raise EvaluationError('Chromium 无法启动；先执行 python -m playwright install chromium，Linux 可加 --with-deps。') from None
    if not healthy(cfg):
        raise EvaluationError('已配置的 OCR 服务不可用；请启动它或明确修改本地配置，未发送模型请求。')


def original_supervisor(*args):
    from ..worker import supervise
    return supervise(*args)


def prepare_case(case: Case, store: Store) -> dict:
    from ..image_input import save_image, MAX_REQUEST
    files = [('question', case.question)] + [('answer', p) for p in case.answers]
    if sum(p.stat().st_size for _, p in files) > MAX_REQUEST:
        raise EvaluationError(f'第 {case.id} 题超过原接口的总上传容量。')
    job_id, directory = store.prepare()
    images, originals = [], []
    for index, (role, path) in enumerate(files, 1):
        with path.open('rb') as stream:
            raw_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
            stream.seek(0)
            normalized = save_image(SimpleNamespace(stream=stream), directory/f'I{index}.png')
        images.append({'id': f'I{index}', 'role': role, 'file': f'I{index}.png', 'sha256': normalized})
        originals.append({'role': role, 'file': path.name, 'sha256': raw_hash})
    write_json(directory/'request.json', {'text': '', 'images': images, 'force': True})
    encoded = json.dumps([{'role': x['role'], 'sha256': x['sha256']} for x in images], sort_keys=True).encode()
    return {'case_id': case.id, 'job_id': job_id, 'status': 'not_started',
            'input_hash': hashlib.sha256(encoded).hexdigest(), 'original_inputs': originals,
            'end_to_end_seconds': None, 'execution_seconds': None, 'model_calls': None,
            'known_output_tokens': None, 'usage_complete': False, 'artifact': None,
            'failure_stage': None, 'error': ''}


def finish_case(row: dict, store: Store, started: float, last_stage: str, out: Path) -> dict:
    from ..observability.timing import summarize
    state = store.get(row['job_id'])
    directory = store.path(row['job_id'])
    report = read_json(directory/'report.json', {})
    audit = read_json(directory/'audit.json', report.get('audit', []))
    elapsed = max(0.0, state['updated']-state['created'])
    perf = summarize({'status': state['status'], 'seconds': max(0, state['updated']-started), 'audit': audit})
    calls = report.get('model_calls_this_job')
    model_rows = [x for x in audit if x.get('kind') == 'model' and not x.get('not_sent')]
    with_usage = [x for x in model_rows if isinstance(x.get('usage'), dict) and x['usage']]
    not_sent = sum(bool(x.get('not_sent')) for x in audit if x.get('kind') == 'model')
    known_output = any(s['calls'] > s['missing_usage'] for s in perf['model_stages'].values())
    complete_output = all(s['missing_usage'] == 0 for s in perf['model_stages'].values())
    result = {**row, 'status': state['status'], 'end_to_end_seconds': round(elapsed, 3),
              'execution_seconds': round(max(0, state['updated']-started), 3),
              'queue_seconds': round(max(0, started-state['created']), 3),
              'model_calls': calls, 'recorded_model_responses': len(model_rows),
              'known_output_tokens': sum(s['completion_tokens'] for s in perf['model_stages'].values()) if known_output else 0 if calls == 0 else None,
              'usage_complete': type(calls) is int and calls-not_sent == len(with_usage) and complete_output,
              'failure_stage': last_stage if state['status'] != 'completed' else None,
              'error': redact(state.get('error') or ''), 'perf': perf,
              'ocr_cache_hits': [x.get('cache_hit') for x in audit if x.get('kind') == 'ocr' and x.get('status') == 'done'],
              'provider_cache': 'See raw usage in local audit; not assumed disabled',
              'fatal_provider_status': next((x.get('status') for x in model_rows if x.get('status') in {401,402,403}), None)}
    local = out/'cases'/row['case_id']
    local.mkdir(parents=True, exist_ok=True)
    artifact = directory/'output/result.html'
    if state['status'] == 'completed':
        if not artifact.is_file():
            result.update(status='failed', error='记录为 completed 但完整网页缺失，未计为交付成功。')
        else:
            shutil.copyfile(artifact, local/'result.html')
            result['artifact'] = f'cases/{row["case_id"]}/result.html'
    write_json(local/'result.json', result)
    return result


def run_batch(cases: list[Case], root: Path, *, env_file: Path | None = None,
              out: Path | None = None, dry_run: bool = False, timeout: int | None = None) -> tuple[Path, int]:
    if os.name != 'posix':
        raise EvaluationError('原 Worker 需要 Linux/macOS；Windows 请在 WSL2 中执行。')
    inherited = load_env(env_file or root/'.env', required=env_file is not None)
    if inherited:
        print('已有环境变量优先于 .env：' + ', '.join(inherited), flush=True)
    if os.getenv('AGENT_WORKFLOW', 'classic') != 'classic':
        raise EvaluationError('当前脚本验收 classic 重构版；请移除 V2 的 AGENT_WORKFLOW 设置或设为 classic。')
    out = (out or root/'eval-runs'/(datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6])).resolve()
    if out.exists():
        raise EvaluationError('输出目录已存在；禁止覆盖或自动续跑付费请求，请换一个新目录。')
    # These changes affect only this process and its children, never production .env.
    os.environ.update(AGENT_DATA_DIR=str(out/'runtime'), REDIS_URL='',
                      LANGSMITH_TRACING='false', LANGCHAIN_TRACING_V2='false')
    if timeout is not None:
        os.environ['AGENT_JOB_TIMEOUT'] = str(timeout)
    out.mkdir(parents=True, mode=0o700)
    from ..config import Settings
    try:
        cfg = Settings()
    except (ValueError, TypeError):
        raise EvaluationError('配置校验失败；请检查本地模型配置与预算，不要使用 shell 展开。') from None
    print(f'题数={len(cases)}；模型={cfg.model}；思考={cfg.effort}；单题时限={cfg.job_timeout}s；单题调用上限={cfg.max_calls}', flush=True)
    print('仅测试数据目录隔离、禁用成品缓存和远端追踪；其他推理策略沿用实际配置。', flush=True)
    if not dry_run:
        preflight(cfg)
    store = Store(cfg.data_dir)
    identity = code_identity(root)
    config = asdict(cfg)
    settings = {k: config[k] for k in PUBLIC_SETTINGS if k in config}
    settings.update(ocr_enabled=bool(cfg.ocr_url), application_cache=False, remote_tracing=False)
    rows = [prepare_case(case, store) for case in cases]  # Validate every input before ANY paid request.
    manifest = {'schema_version': 'simple-eval-v1', 'code': identity, 'settings': settings,
                'selected_cases': [c.id for c in cases], 'dry_run': dry_run,
                'timing_scope': 'enqueue to artifact ready; input preparation is outside per-task timing',
                'input_roles': 'question + optional reference answer (not trusted gold)',
                'budget_note': 'one bounded attempt per selected case; no automatic repeat'}
    write_json(out/'manifest.json', manifest)
    if dry_run:
        for row in rows:
            row['status'] = 'prepared'
        write_json(out/'runs.json', rows)
        render_report(out)
        print(f'仅准备，模型请求=0。输出：{out}', flush=True)
        return out, 0
    write_json(out/'runs.json', rows)
    stopped = False
    def stop(*_):
        nonlocal stopped
        stopped = True
    previous = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    for s in previous:
        signal.signal(s, stop)
    try:
        for index, row in enumerate(rows):
            if stopped:
                break
            if code_identity(root)['code_sha256'] != identity['code_sha256']:
                raise EvaluationError('执行期间代码发生变化；停止启动后续题，已有结果保留。')
            store.enqueue(row['job_id'])
            if store.claim() != row['job_id']:
                raise EvaluationError('隔离任务队列异常；未启动模型。')
            row['status'] = 'running'
            write_json(out/'runs.json', rows)
            started = time.time()
            last_stage = 'starting'
            def progress():
                nonlocal last_stage
                current = store.get(row['job_id'])['stage']
                if current != last_stage and current not in {'completed','failed','interrupted','cancelled','needs_input'}:
                    last_stage = current
                    print(f'[{index+1}/{len(rows)} 题{row["case_id"]}] {current} ({time.time()-started:.0f}s)', flush=True)
            try:
                original_supervisor(cfg, store, row['job_id'], lambda: stopped, progress)
            except Exception as exc:
                store.finish(row['job_id'], 'failed', error='评测调度异常：'+type(exc).__name__)
            try:
                rows[index] = finish_case(row, store, started, last_stage, out)
            except Exception as exc:
                rows[index] = {**row, 'status': 'failed', 'error': '结果采集异常：'+type(exc).__name__}
            write_json(out/'runs.json', rows)
            render_report(out)
            result = rows[index]
            print(f'[{index+1}/{len(rows)} 题{row["case_id"]}] {result["status"]}，耗时={result.get("end_to_end_seconds")}s，调用={result.get("model_calls")}', flush=True)
            if result.get('error'):
                print('  '+result['error'][:300], flush=True)
            if result.get('fatal_provider_status'):
                print('鉴权/余额/权限错误，停止剩余题；没有自动重试。', flush=True)
                break
    finally:
        for s, handler in previous.items():
            signal.signal(s, handler)
        write_json(out/'runs.json', rows)
        render_report(out)
    return out, 130 if stopped else 0 if all(r['status'] == 'completed' for r in rows) else 1
