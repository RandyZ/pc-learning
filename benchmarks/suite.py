"""One-command, opt-in comparison of numbered question/answer image pairs.

This module uses only the standard library until the existing benchmarks.run
child is started. A preview never imports the agent or calls a model. Each paid
job is a fresh child with the same base environment; there is no retry/resume.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import shlex
import signal
import statistics
import subprocess
import sys
import time
from urllib.parse import quote
import uuid

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ("A1", "B0", "B1", "B2")
MODEL_FIELDS = ("model", "base_url", "api_style", "thinking", "effort", "review_effort",
                "diagram_thinking", "max_tokens", "solve_max_tokens", "review_max_tokens",
                "source_max_tokens", "max_calls", "max_repairs", "job_timeout", "ocr_profile", "ocr_timeout")
IMAGE_NAME = re.compile(r"^(\d+)(答案)?\.(png|jpg|jpeg|webp)$", re.IGNORECASE)


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def load_env(path: Path, inherited: dict[str, str]) -> dict[str, str]:
    """Read single-line dotenv values as data, never source shell commands.

    Exported variables take precedence, including empty values. No interpolation
    or command substitution is performed; diagnostics never include values.
    """
    values: dict[str, str] = {}
    if path.exists():
        for line_no, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, sep, value = line.partition("=")
            key, value = key.strip(), value.strip()
            try:
                if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                    raise ValueError
                if value.startswith(("'", '"')):
                    parts = shlex.split(value, comments=True, posix=True)
                    if len(parts) != 1:
                        raise ValueError
                    value = parts[0]
                else:
                    value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
                if "\x00" in value:
                    raise ValueError
                values[key] = value
            except ValueError:
                raise ValueError(f".env 第 {line_no} 行格式错误；只支持单行 KEY=VALUE，不执行 Shell。") from None
    return {**values, **inherited}


def redact(text: str, env: dict[str, str]) -> str:
    secrets = [value for key, value in env.items() if len(value) >= 6 and
               any(word in key.upper() for word in ("KEY", "TOKEN", "PASSWORD", "SECRET"))]
    for value in sorted(secrets, key=len, reverse=True):
        text = text.replace(value, "[REDACTED]")
    return re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "[REDACTED]", text)


@dataclass(frozen=True)
class Case:
    number: int
    question: Path
    answer: Path | None

    @property
    def id(self) -> str:
        return f"{self.number:02d}"


def discover(directory: Path, selectors: list[str], without_answer: bool = False) -> list[Case]:
    if not directory.is_dir():
        raise ValueError(f"题库目录不存在：{directory}。图片推送完成后再运行。")
    pairs: dict[int, dict[str, Path]] = {}
    for path in directory.iterdir():
        match = IMAGE_NAME.fullmatch(path.name)
        if not path.is_file() or not match:
            continue
        if not path.resolve().is_relative_to(directory.resolve()):
            raise ValueError(f"题图链接不能指向题库目录之外：{path.name}")
        number, role = int(match[1]), "answer" if match[2] else "question"
        if number < 1:
            raise ValueError("题号必须从 1 开始。")
        slot = pairs.setdefault(number, {})
        if role in slot:
            raise ValueError(f"题号 {number:02d} 存在重复的{role}文件，请保留一份。")
        slot[role] = path.resolve()
    available = {n for n, pair in pairs.items() if "question" in pair}
    tokens = [t.strip() for part in selectors for t in part.split(",") if t.strip()]
    if tokens == ["all"]:
        orphaned = sorted(set(pairs) - available)
        if orphaned:
            raise ValueError("发现只有答案、缺少题图的编号：" + ",".join(f"{n:02d}" for n in orphaned))
        selected = sorted(available)
    else:
        selected_set: set[int] = set()
        for token in tokens:
            match = re.fullmatch(r"(\d+)(?:-(\d+))?", token)
            if not match:
                raise ValueError("题号格式：01、01 02、01,02、01-03 或 all。")
            start, end = int(match[1]), int(match[2] or match[1])
            if start < 1 or end < start or end - start > 9999:
                raise ValueError("题号区间无效或过大。")
            selected_set.update(range(start, end + 1))
        selected = sorted(selected_set)
    if not selected:
        raise ValueError("没有发现编号题图，例如 01.png；不把空目录当成测试成功。")
    result = []
    for number in selected:
        pair = pairs.get(number, {})
        if "question" not in pair:
            raise ValueError(f"缺少题图：{number:02d}.png")
        if "answer" not in pair and not without_answer:
            raise ValueError(f"缺少答案图：{number:02d}答案.png。只测题图需显式加 --without-answer。")
        result.append(Case(number, pair["question"], None if without_answer else pair["answer"]))
    return result


def make_plan(cases: list[Case], profiles: list[str]) -> dict:
    rows = []
    for index, case in enumerate(cases):
        # Alternate AB/BA over cases, without running two Workers together.
        offset = index % len(profiles)
        for profile in profiles[offset:] + profiles[:offset]:
            rows.append({"case_id": case.id, "profile": profile, "relative_dir": f"{case.id}/{profile}",
                         "question": str(case.question), "question_sha256": file_hash(case.question),
                         "answer": str(case.answer) if case.answer else None,
                         "answer_sha256": file_hash(case.answer) if case.answer else None,
                         "launcher_status": "not_run", "return_code": None})
    return {"schema_version": 1, "created_at": datetime.now().astimezone().isoformat(),
            "profiles": profiles, "runs": rows, "automatic_retry": False}


def atomic_json(path: Path, value: object) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON object required")
    return value


def local_link(path: Path, root: Path) -> str | None:
    if path.is_file() and path.resolve().is_relative_to(root.resolve()):
        return quote(path.relative_to(root).as_posix(), safe="/")
    return None


def report(root: Path) -> dict:
    plan = read_json(root / "suite.json")
    rows, warnings, comparison_inputs = [], [], {}
    for item in plan["runs"]:
        # Existing result files are data, not authority to read arbitrary local paths.
        case, profile = item["case_id"], item["profile"]
        if not re.fullmatch(r"\d{2,}", case) or profile not in PROFILES:
            raise ValueError("suite.json 包含非法题号或配置。")
        directory = root / case / profile
        if not directory.resolve().is_relative_to(root.resolve()):
            raise ValueError("任务结果目录不能指向本轮目录之外。")
        result_file = directory / "result.json"
        row = {"case_id": case, "profile": profile, "status": item["launcher_status"],
               "seconds": None, "model_calls": None, "output_tokens": None,
               "quality": "未人工评审", "links": {}}
        if result_file.exists():
            try:
                if not result_file.resolve().is_relative_to(root.resolve()):
                    raise ValueError("result file points outside this run")
                result = read_json(result_file)
                if result.get("case_id") != case or result.get("profile") != profile:
                    raise ValueError("result identity mismatch")
                row["status"] = str(result.get("status", "unknown"))
                row["seconds"] = number(result.get("end_to_end_seconds"))
                row["model_calls"] = number(result.get("model_calls"))
                stages = result.get("perf", {}).get("model_stages", {})
                if stages and all(not s.get("missing_usage") for s in stages.values()):
                    row["output_tokens"] = sum(number(s.get("output_tokens")) or 0 for s in stages.values())
                # Absence of a final call count is unknown, not zero.
                job = result.get("job_id", "")
                if re.fullmatch(r"[0-9a-f]{32}", job):
                    job_dir = directory / "runtime" / "jobs" / job
                    for label, path in (("任务报告", job_dir / "report.json"), ("审计", job_dir / "audit.json")):
                        link = local_link(path, root)
                        if link:
                            row["links"][label] = link
                    if row["status"] == "completed":
                        link = local_link(job_dir / "output" / "result.html", root)
                        if link:
                            row["links"]["讲解网页"] = link
                row["links"]["统计JSON"] = local_link(result_file, root)
                comparison_inputs[(case, profile)] = result
            except (OSError, ValueError, TypeError, AttributeError):
                row["status"] = "invalid_result"
                warnings.append(f"{case}/{profile}：结果损坏或身份不匹配，不计为成功。")
        elif item["launcher_status"] == "finished":
            row["status"] = "missing_result"
        elif item["launcher_status"] == "running":
            row["status"] = "unknown_completion"
            warnings.append(f"{case}/{profile}：在途结果不明；不得自动重试，已发请求可能计费。")
        log = local_link(root / "logs" / f"{case}_{profile}.log", root)
        if log:
            row["links"]["控制台日志"] = log
        rows.append(row)
    for case in sorted({r["case_id"] for r in rows}):
        results = [r for (c, _), r in comparison_inputs.items() if c == case]
        for key in MODEL_FIELDS:
            if len({json.dumps(r.get("effective_config", {}).get(key), sort_keys=True) for r in results}) > 1:
                warnings.append(f"{case}：{key} 不一致，不能视为严格的同配置对照。")
        if len({r.get("input_hash") for r in results}) > 1:
            warnings.append(f"{case}：实际输入哈希不同。")
    groups = {}
    for profile in plan["profiles"]:
        values = [r for r in rows if r["profile"] == profile]
        completed = [r for r in values if r["status"] == "completed"]
        seconds = [r["seconds"] for r in completed if r["seconds"] is not None]
        groups[profile] = {"planned": len(values), "completed": len(completed),
                           "not_run": sum(r["status"] == "not_run" for r in values),
                           "failed_or_unknown": sum(r["status"] not in {"completed", "not_run"} for r in values),
                           "completed_latency_median_seconds": statistics.median(seconds) if seconds else None}
    summary = {"runs": rows, "groups": groups, "warnings": warnings,
               "note": "完成≠数学正确；时延中位数仅统计完成项，失败/未运行数量另列。未完成人工评审，不宣称正确率或提速达标。"}
    atomic_json(root / "summary.json", summary)
    headers = ["题号", "配置", "状态", "秒", "模型请求", "输出Token", "结果"]
    md = ["# 本地对照测试", "", summary["note"], "", "| " + " | ".join(headers) + " |",
          "|---|---|---|---:|---:|---:|---|"]
    table = []
    for row in rows:
        values = [row["case_id"], row["profile"], row["status"],
                  f"{row['seconds']:.1f}" if row["seconds"] is not None else "—",
                  str(row["model_calls"]) if row["model_calls"] is not None else "—",
                  str(row["output_tokens"]) if row["output_tokens"] is not None else "—"]
        links = {k: v for k, v in row["links"].items() if v}
        md.append("| " + " | ".join(v.replace("|", "\\|").replace("\n", " ") for v in values) + " | " +
                  " · ".join(f"[{k}]({v})" for k, v in links.items()) + " |")
        table.append("<tr>" + "".join(f"<td>{html.escape(v)}</td>" for v in values) + "<td>" +
                     " · ".join(f'<a href="{html.escape(v, quote=True)}">{html.escape(k)}</a>' for k, v in links.items()) + "</td></tr>")
    md += ["", "## 配置汇总", "", "| 配置 | 完成/计划 | 失败或不明 | 未运行 | 完成项耗时中位数（秒） |",
           "|---|---:|---:|---:|---:|"]
    group_html = []
    for profile, group in groups.items():
        median = group["completed_latency_median_seconds"]
        values = [profile, f"{group['completed']}/{group['planned']}", str(group["failed_or_unknown"]),
                  str(group["not_run"]), f"{median:.1f}" if median is not None else "—"]
        md.append("| " + " | ".join(values) + " |")
        group_html.append("<tr>" + "".join("<td>" + html.escape(v) + "</td>" for v in values) + "</tr>")
    if warnings:
        md += ["", "## 对照警告", ""] + warnings
    md += ["", "本轮文件包含题目、答案和本地任务记录；不要上传公开仓库。"]
    (root / "summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    document = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
    document += '<title>本地对照测试</title><style>body{font-family:system-ui,sans-serif;margin:2rem}table{border-collapse:collapse;width:100%}th,td{padding:.7rem;border:1px solid #ddd;text-align:left}p{line-height:1.7}a{white-space:nowrap}</style>'
    document += "<h1>本地对照测试</h1><p>" + html.escape(summary["note"]) + "</p><table><tr>"
    document += "".join(f"<th>{html.escape(h)}</th>" for h in headers) + "</tr>" + "".join(table) + "</table>"
    document += "<h2>配置汇总</h2><table><tr><th>配置</th><th>完成/计划</th><th>失败或不明</th><th>未运行</th><th>完成项耗时中位数（秒）</th></tr>" + "".join(group_html) + "</table>"
    document += "".join("<p>" + html.escape(w) + "</p>" for w in warnings)
    document += "<p>这是私有本地报告，包含题目和答案产物链接；不要公开上传。费用未知，不自动估成零。</p></html>"
    (root / "summary.html").write_text(document, encoding="utf-8")
    return summary


# Validate every profile before the first paid job. No remote checks or model
# requests: imports, Settings and browser startup/file navigation only.
PREFLIGHT = r'''
import json, os, sys, tempfile
from pathlib import Path
from math_agent.config import Settings
from math_agent import cli
from benchmarks.profiles import overrides
from PIL import Image
from playwright.sync_api import sync_playwright
config = json.loads(sys.stdin.read())
base = dict(os.environ)
for profile in config['profiles']:
    os.environ.clear(); os.environ.update(base); os.environ.update(overrides(profile))
    os.environ.update(REDIS_URL='', AGENT_DATA_DIR=str(Path(tempfile.gettempdir()) / 'pc-learning-preflight'))
    Settings()
for name in config['images']:
    with Image.open(name) as image:
        image.verify()
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, executable_path=base.get('MATH_AGENT_CHROMIUM_EXECUTABLE') or base.get('GGB_CHROMIUM_EXECUTABLE'))
    try:
        with tempfile.TemporaryDirectory() as directory:
            page_file = Path(directory) / 'check.html'
            page_file.write_text('<p>local preflight</p>')
            browser.new_page().goto(page_file.as_uri(), wait_until='load', timeout=10000)
    finally:
        browser.close()
print('PREFLIGHT_OK')
'''


def preflight(env: dict[str, str], plan: dict) -> None:
    payload = {"profiles": plan["profiles"], "images": sorted({r[k] for r in plan["runs"] for k in ("question", "answer") if r[k]})}
    try:
        checked = subprocess.run([sys.executable, "-c", PREFLIGHT], cwd=ROOT, env=env,
                                 input=json.dumps(payload), text=True, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, timeout=60)
    except subprocess.TimeoutExpired:
        raise ValueError("依赖/配置/本地浏览器预检超时；尚未调用模型。") from None
    if checked.returncode:
        raise ValueError("预检失败；尚未调用模型。请检查依赖、配置、图片和 Chromium：\n" + redact(checked.stdout, env)[-6000:])


@contextmanager
def checkout_lock():
    # The original Worker is POSIX-only as well. Lock across suites in this checkout.
    import fcntl
    folder = ROOT / "benchmark-runs"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / ".suite.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("同一工程已有一轮对照测试在运行；请勿并发争抢资源。") from None
        yield


class Stop:
    requested = False

    def __init__(self):
        self.child: subprocess.Popen | None = None
        self.requested = False

    def handle(self, *_):
        self.requested = True
        if self.child is not None and self.child.poll() is None:
            try:
                # The original benchmark supervisor cleans its own job process group.
                self.child.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass


def launch(command: list[str], env: dict[str, str], stop: Stop) -> tuple[int, str]:
    child = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                             start_new_session=True)
    stop.child = child
    if stop.requested:
        stop.handle()
    try:
        output, _ = child.communicate()
        return child.returncode, redact(output, env)
    finally:
        stop.child = None


def execute(plan: dict, out: Path, env: dict[str, str]) -> int:
    stop = Stop()
    old_handlers = {s: signal.signal(s, stop.handle) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        for index, item in enumerate(plan["runs"], 1):
            if stop.requested:
                break
            for path_key, hash_key in (("question", "question_sha256"), ("answer", "answer_sha256")):
                if item[path_key] and file_hash(Path(item[path_key])) != item[hash_key]:
                    raise ValueError("本轮输入文件已改变，停止后续测试；不会自动重试已运行任务。")
            directory = out / item["relative_dir"]
            command = [sys.executable, "-m", "benchmarks.run", "--profile", item["profile"],
                       "--case-id", item["case_id"], "--question", item["question"], "--out", str(directory), "--allow-paid"]
            if item["answer"]:
                command += ["--answer", item["answer"]]
            print(f"[{index}/{len(plan['runs'])}] 题 {item['case_id']} / {item['profile']} 开始（Ctrl+C 停止后续任务）", flush=True)
            item["launcher_status"] = "running"
            atomic_json(out / "suite.json", plan)
            try:
                code, output = launch(command, dict(env), stop)
            except OSError as exc:
                code, output = 127, type(exc).__name__
            item.update(launcher_status="finished", return_code=code)
            (out / "logs" / f"{item['case_id']}_{item['profile']}.log").write_text(output, encoding="utf-8")
            atomic_json(out / "suite.json", plan)
            summary = report(out)
            row = summary["runs"][index - 1]
            print(f"  {row['status']} | {row['seconds'] if row['seconds'] is not None else '未知'} 秒 | 模型请求 {row['model_calls'] if row['model_calls'] is not None else '未知'}", flush=True)
            if row["status"] in {"interrupted", "cancelled"}:
                stop.requested = True
            # A task-level failure is part of the comparison. A launcher failure
            # or invalid result stops the batch rather than consuming more budget.
            if row["status"] in {"missing_result", "invalid_result", "unknown_completion"}:
                print("运行器没有提供可信结果，停止后续任务；检查本地日志。", flush=True)
                break
    finally:
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)
        atomic_json(out / "suite.json", plan)
        summary = report(out)
        print(f"\n对比报告：{out / 'summary.html'}\n机器可读：{out / 'summary.json'}", flush=True)
    if stop.requested:
        return 130
    return 0 if all(r["status"] == "completed" for r in summary["runs"]) else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="自动配对 tests/test-items 的题图和答案图，串行对比旧流程与 V2。默认仅预览，不花费模型额度。")
    parser.add_argument("cases", nargs="*", default=["01"], help="01、01 02、01-03 或 all；默认01")
    parser.add_argument("--profiles", default="A1,B0", help="逗号分隔：A1,B0,B1,B2；默认A1,B0")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "tests/test-items")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--without-answer", action="store_true", help="显式只用题图，不发送答案图")
    parser.add_argument("--run", action="store_true", help="授权本轮所列的新任务调用真实模型；可能收费，不自动重试")
    parser.add_argument("--out", type=Path, help="本轮全新输出目录，禁止覆盖/恢复")
    parser.add_argument("--report", type=Path, help="仅重建已运行目录的汇总，不调用模型，也不重跑任务")
    args = parser.parse_args(argv)
    try:
        if args.report:
            if args.run:
                raise ValueError("--report 不能与 --run 同时使用。")
            report(args.report.resolve())
            print(args.report.resolve() / "summary.html")
            return 0
        profiles = [p.strip().upper() for p in args.profiles.split(",")]
        if not profiles or len(profiles) != len(set(profiles)) or not set(profiles) <= set(PROFILES):
            raise ValueError("配置必须为 A1,B0,B1,B2 的不重复列表。")
        cases = discover(args.data_dir.resolve(), args.cases or ["01"], args.without_answer)
        plan = make_plan(cases, profiles)
        print(f"题目 {len(cases)} 道；配置 {','.join(profiles)}；共 {len(plan['runs'])} 个全新任务，严格串行，不自动重试。")
        for case in cases:
            print(f"  {case.id}: {case.question.name} + {case.answer.name if case.answer else '不传答案'}")
        print("A1=classic兼容路径（不是完整重构版）；B0=V2串行；B1=V2并行；B2=V2并行+计算复用。")
        if not args.run:
            print("当前仅预览，模型调用为0。确认后在同一命令末尾加 --run。")
            return 0
        if os.name != "posix":
            raise ValueError("测试调用原 Worker，需要 macOS/Linux 或 WSL。")
        env = load_env(args.env_file.resolve(), dict(os.environ))
        if not env.get("DEEPSEEK_API_KEY", "").strip():
            raise ValueError("请在本地 .env 或环境变量设置 DEEPSEEK_API_KEY；不要放入命令参数。")
        out = (args.out or ROOT / "benchmark-runs" / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])).resolve()
        if out.exists():
            raise ValueError("输出目录已经存在；不覆盖、不自动续跑。查看已有结果请用 --report。")
        with checkout_lock():
            print("正在检查全部配置、图片及本地浏览器；此检查不调用模型。", flush=True)
            preflight(env, plan)
            out.mkdir(parents=True, mode=0o700, exist_ok=False)
            (out / "logs").mkdir(mode=0o700)
            atomic_json(out / "suite.json", plan)
            report(out)
            return execute(plan, out, env)
    except (OSError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
