"""Fixture discovery and a non-executable, deliberately small dotenv reader."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import re
import shlex


class EvaluationError(ValueError):
    """Safe-to-display local validation error (never includes credential values)."""


@dataclass(frozen=True)
class Case:
    number: int
    question: Path
    answers: tuple[Path, ...]

    @property
    def id(self) -> str:
        return f'{self.number:02d}'


def discover(directory: Path) -> dict[int, Case]:
    if not directory.is_dir():
        raise EvaluationError('找不到题库目录；默认使用 tests/test-items，可用 --fixtures 指定。')
    questions: dict[int, Path] = {}
    answers: dict[int, dict[int, Path]] = {}
    pattern = re.compile(r'([0-9]+)(?:(答案|[_-]answer)(?:[_-]([0-9]+))?)?', re.I)
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix.lower() not in {'.png', '.jpg', '.jpeg', '.webp'}:
            continue
        match = pattern.fullmatch(path.stem)
        if not match:
            raise EvaluationError('题库图片命名不明确；使用 01.png、01答案.png 或 01答案-2.png。')
        if path.is_symlink():
            raise EvaluationError('题库不接受图片符号链接，请使用真实文件。')
        number = int(match[1])
        if number < 1:
            raise EvaluationError('题号必须是正整数。')
        if match[2]:
            part = int(match[3] or 0)
            group = answers.setdefault(number, {})
            if part in group:
                raise EvaluationError(f'第 {number} 题的答案图编号重复。')
            group[part] = path
        else:
            if number in questions:
                raise EvaluationError(f'第 {number} 题的题图重复。')
            questions[number] = path
    if set(answers) - set(questions):
        raise EvaluationError('存在答案图却缺少题图，题号：' + ','.join(map(str, sorted(set(answers)-set(questions)))))
    if not questions:
        raise EvaluationError('没有找到编号题图。')
    result = {}
    for n, path in sorted(questions.items()):
        pages = tuple(p for _, p in sorted(answers.get(n, {}).items()))
        if len(pages) > 5:
            raise EvaluationError(f'第 {n} 题超过原接口六张图片限制。')
        result[n] = Case(n, path, pages)
    return result


def select(expression: str, cases: dict[int, Case]) -> list[Case]:
    if expression.lower() == 'all':
        return list(cases.values())
    numbers: set[int] = set()
    for item in expression.split(','):
        match = re.fullmatch(r'\s*([0-9]+)(?:\s*-\s*([0-9]+))?\s*', item)
        if not match:
            raise EvaluationError('题号格式：1、1-5、1,3,5-8 或 all。')
        lo, hi = int(match[1]), int(match[2] or match[1])
        if lo < 1 or hi < lo or hi > max(cases):
            raise EvaluationError('题号范围无效或超出当前题库。')
        numbers.update(range(lo, hi+1))
    missing = numbers - cases.keys()
    if missing:
        raise EvaluationError('找不到题号：' + ','.join(map(str, sorted(missing))))
    return [cases[n] for n in sorted(numbers)]


def load_env(path: Path, *, required: bool = False) -> list[str]:
    """Environment wins. No source/eval, expansion, commands or multiline values."""
    if not path.exists():
        if required:
            raise EvaluationError('指定的环境配置文件不存在。')
        return []
    parsed = {}
    for number, line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:].lstrip()
        match = re.fullmatch(r'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)', line)
        if not match:
            raise EvaluationError(f'环境文件第 {number} 行不是 KEY=value。')
        key, raw = match.groups()
        if not (key.startswith(('AGENT_', 'DEEPSEEK_', 'LANGSMITH_')) or key in {
            'REDIS_URL', 'LANGCHAIN_TRACING_V2', 'MATH_AGENT_CHROMIUM_EXECUTABLE', 'GGB_CHROMIUM_EXECUTABLE'
        }):
            continue
        if key in parsed:
            raise EvaluationError(f'环境文件第 {number} 行重复定义 {key}。')
        try:
            lexer = shlex.shlex(raw, posix=True)
            lexer.whitespace_split = True
            lexer.commenters = '#'
            parts = list(lexer)
            if len(parts) > 1:
                raise ValueError()
            value = parts[0] if parts else ''
            if '$' in value or '`' in value:
                raise ValueError()
        except ValueError:
            raise EvaluationError(f'环境文件第 {number} 行 {key} 需使用单行字面值；不支持变量或命令展开。') from None
        parsed[key] = value
    inherited = sorted(k for k in parsed if k in os.environ and os.environ[k] != parsed[k])
    for key, value in parsed.items():
        os.environ.setdefault(key, value)
    return inherited
