"""Simple numbered-case evaluation CLI. `run` explicitly authorizes paid requests."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .inputs import EvaluationError, discover, select
from .reporting import compare, render_report

ROOT = Path(__file__).resolve().parents[2]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='数学题本地评测；run 会按所选题数调用付费模型，--dry-run 不调用。')
    sub = parser.add_subparsers(dest='command', required=True)
    listing = sub.add_parser('list', help='列出题图与自动配对的答案图，不调用模型')
    run = sub.add_parser('run', help='显式执行所选题；如 run 1 或 run 1,3,5-8')
    for command in (listing, run):
        command.add_argument('--fixtures', type=Path, default=ROOT/'tests/test-items')
    run.add_argument('cases', help='1、1-5、1,3,5-8 或 all')
    run.add_argument('--env', type=Path, help='默认自动读取项目根目录 .env；已有环境变量优先')
    run.add_argument('--out', type=Path, help='必须是新目录；默认自动生成 eval-runs/时间戳')
    run.add_argument('--dry-run', action='store_true', help='只准备图片和配置，零模型请求')
    run.add_argument('--budget-mode', choices=('validation', 'production'), default='validation',
                     help='默认validation跑完整结果，不沿用生产预算；production用于受限时延对照')
    run.add_argument('--max-output-tokens', type=int,
                     help='验证模式手动指定供应商输出上限；默认通过/models读取，不使用生产低额度')
    run.add_argument('--timeout', type=int, help='显式单题秒数；validation默认无截止，production默认沿用AGENT_JOB_TIMEOUT')
    report = sub.add_parser('report', help='重建报告/应用 quality.csv 人工评分，不调用模型')
    report.add_argument('directory', type=Path)
    comparison = sub.add_parser('compare', help='对比本脚本的两次运行，不调用模型')
    comparison.add_argument('left', type=Path)
    comparison.add_argument('right', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == 'list':
            cases = discover(args.fixtures.resolve())
            for case in cases.values():
                print(f'{case.id}: {case.question.name} -> ' + (', '.join(p.name for p in case.answers) or '无参考答案'))
            print(f'共 {len(cases)} 题。')
            return 0
        if args.command == 'report':
            value = render_report(args.directory.resolve())
            print(json.dumps(value, ensure_ascii=False, indent=2))
            print('网页：' + str((args.directory/'report.html').resolve()))
            return 0
        if args.command == 'compare':
            print(json.dumps(compare(args.left.resolve(), args.right.resolve()), ensure_ascii=False, indent=2))
            return 0
        from .runner import run_batch
        cases = select(args.cases, discover(args.fixtures.resolve()))
        os.umask(0o077)
        out, code = run_batch(cases, ROOT, env_file=args.env, out=args.out,
                             dry_run=args.dry_run, timeout=args.timeout,
                             budget_mode=args.budget_mode, max_output_tokens=args.max_output_tokens)
        print('报告：' + str(out/'report.html'))
        return code
    except EvaluationError as exc:
        print('错误：' + str(exc))
        return 2
    except Exception as exc:
        # Never echo third-party exception payloads that might include credentials.
        print('本地评测异常：' + type(exc).__name__ + '；检查依赖、图片和配置。已有输出不会自动重跑。')
        return 2
