from __future__ import annotations
from dataclasses import dataclass, field
import os
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    workflow: str = field(default_factory=lambda: os.getenv("AGENT_WORKFLOW", "classic"))
    v2_profile: str = field(default_factory=lambda: os.getenv("AGENT_V2_PROFILE", "serial"))
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("AGENT_DATA_DIR", str(ROOT / "data"))))
    api_key: str = field(default_factory=lambda: os.getenv("DEEPSEEK_API_KEY", ""), repr=False)
    access_token: str = field(default_factory=lambda: os.getenv("AGENT_ACCESS_TOKEN", ""), repr=False)
    base_url: str = field(default_factory=lambda: os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))
    model: str = field(default_factory=lambda: os.getenv("DEEPSEEK_MODEL", "deepseek-flash"))
    api_style: str = field(default_factory=lambda: os.getenv("AGENT_API_STYLE", "deepseek"))
    review_responses: bool = field(default_factory=lambda: os.getenv("AGENT_REVIEW_RESPONSES", "false").lower() == "true")
    solve_responses: bool = field(default_factory=lambda: os.getenv("AGENT_SOLVE_RESPONSES", "false").lower() == "true")
    overlap_ocr: bool = field(default_factory=lambda: os.getenv("AGENT_OVERLAP_OCR", "false").lower() == "true")
    native_constructions: bool = field(default_factory=lambda: os.getenv("AGENT_NATIVE_CONSTRUCTIONS", "false").lower() == "true")
    incremental_math_review: bool = field(default_factory=lambda: os.getenv("AGENT_INCREMENTAL_MATH_REVIEW", "false").lower() == "true")
    incremental_review: bool = field(default_factory=lambda: os.getenv("AGENT_INCREMENTAL_REVIEW", "false").lower() == "true")
    compact_tools: bool = field(default_factory=lambda: os.getenv("AGENT_COMPACT_TOOLS", "false").lower() == "true")
    parallel_questions: bool = field(default_factory=lambda: os.getenv("AGENT_PARALLEL_QUESTIONS", "false").lower() == "true")
    parallel_review: bool = field(default_factory=lambda: os.getenv("AGENT_PARALLEL_REVIEW", "false").lower() == "true")
    question_concurrency: int = field(default_factory=lambda: int(os.getenv("AGENT_QUESTION_CONCURRENCY", "2")))
    solve_draft: bool = field(default_factory=lambda: os.getenv("AGENT_SOLVE_DRAFT", "false").lower() == "true")
    review_plan: bool = field(default_factory=lambda: os.getenv("AGENT_REVIEW_PLAN", "false").lower() == "true")
    reference_mode: bool = field(default_factory=lambda: os.getenv("AGENT_REFERENCE_MODE", "false").lower() == "true")
    thinking: bool = field(default_factory=lambda: os.getenv("DEEPSEEK_THINKING", "true").lower() == "true")
    diagram_thinking: bool = field(default_factory=lambda: os.getenv("DEEPSEEK_DIAGRAM_THINKING", "false").lower() == "true")
    effort: str = field(default_factory=lambda: os.getenv("DEEPSEEK_REASONING_EFFORT", "high"))
    review_effort: str = field(default_factory=lambda: os.getenv("AGENT_REVIEW_EFFORT", ""))
    max_tokens: int = field(default_factory=lambda: int(os.getenv("DEEPSEEK_MAX_TOKENS", "12000")))
    solve_max_tokens: int = field(default_factory=lambda: int(os.getenv("DEEPSEEK_SOLVE_MAX_TOKENS", "0")))
    review_max_tokens: int = field(default_factory=lambda: int(os.getenv("DEEPSEEK_REVIEW_MAX_TOKENS", "0")))
    review_reserve_seconds: int = field(default_factory=lambda: int(os.getenv("AGENT_REVIEW_RESERVE_SECONDS", "0")))
    source_max_tokens: int = field(default_factory=lambda: int(os.getenv("DEEPSEEK_SOURCE_MAX_TOKENS", "8000")))
    ocr_url: str = field(default_factory=lambda: os.getenv("AGENT_OCR_URL", "").rstrip("/"))
    ocr_profile: str = field(default_factory=lambda: os.getenv("AGENT_OCR_PROFILE", "ppstructure3-v1"))
    ocr_timeout: int = field(default_factory=lambda: int(os.getenv("AGENT_OCR_TIMEOUT", "240")))
    job_timeout: int = field(default_factory=lambda: int(os.getenv("AGENT_JOB_TIMEOUT", "240")))
    max_calls: int = field(default_factory=lambda: int(os.getenv("AGENT_MAX_MODEL_CALLS", "10")))
    http_timeout: int = field(default_factory=lambda: int(os.getenv("DEEPSEEK_HTTP_TIMEOUT", "300")))
    max_repairs: int = field(default_factory=lambda: int(os.getenv("AGENT_MAX_REPAIRS", "2")))
    redis_url: str = field(default_factory=lambda: os.getenv("REDIS_URL", ""))
    cache_ttl: int = field(default_factory=lambda: int(os.getenv("AGENT_CACHE_TTL", "604800")))

    def __post_init__(self):
        if self.workflow not in {"classic", "evidence_v2"}:
            raise ValueError("AGENT_WORKFLOW must be classic or evidence_v2")
        if self.v2_profile not in {"serial", "parallel", "parallel_reuse"}:
            raise ValueError("AGENT_V2_PROFILE must be serial, parallel or parallel_reuse")
        if self.workflow == "evidence_v2":
            if self.api_style != "deepseek" or not self.compact_tools:
                raise ValueError("V2 requires DeepSeek and AGENT_COMPACT_TOOLS=true")
            if any((self.reference_mode, self.review_plan, self.solve_draft,
                    self.parallel_questions, self.parallel_review, self.overlap_ocr,
                    self.incremental_review, self.incremental_math_review)):
                raise ValueError("V2 first-release experiments cannot mix reference/draft/parallel-model/OCR-overlap/incremental-review flags")
        if self.parallel_review and (not self.compact_tools or self.reference_mode or self.review_plan or self.api_style!='deepseek'):
            raise ValueError('分问复核仅支持compact classic流程，不能叠加reference或review_plan')
        if self.parallel_review and self.incremental_review:
            raise ValueError('分问复核暂不叠加仅修图复核复用；修改后须重新分问复核')
        if self.solve_responses and (self.api_style!='deepseek' or not self.compact_tools):
            raise ValueError('原生解题Schema仅支持DeepSeek compact流程')
        if type(self.question_concurrency) is not int or not 1 <= self.question_concurrency <= 2:
            raise ValueError('小问实验并发只能为1或2')
        if self.parallel_questions and (not self.compact_tools or self.reference_mode or self.solve_draft or self.api_style!='deepseek'):
            raise ValueError('小问并发仅支持compact classic流程，不能叠加reference或solve_draft')
        if any(v != 0 and not 512 <= v <= 64000 for v in (self.solve_max_tokens, self.review_max_tokens)):
            raise ValueError("阶段输出额度须为0（沿用全局）或512～64000")
        if not 0 <= self.review_reserve_seconds <= self.job_timeout - 30:
            raise ValueError("复核预留时限必须非负，并给前序保留至少30秒")
        if not 512 <= self.source_max_tokens <= 64000 or not 10 <= self.ocr_timeout <= 600:
            raise ValueError("OCR时限或读题输出额度超出范围")
        if self.ocr_url:
            ocr = urlparse(self.ocr_url)
            if ocr.scheme != "http" or ocr.hostname not in {"127.0.0.1", "localhost", "ocr"} or ocr.username or ocr.password or ocr.query or ocr.fragment or ocr.path:
                raise ValueError("OCR只能使用本机或私有ocr服务HTTP地址")
        url = urlparse(self.base_url)
        if url.scheme != "https" and not (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1"}):
            raise ValueError("模型地址必须为 HTTPS，或本机 HTTP 测试地址")
        if not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("模型地址不能包含凭据、查询或片段")
        if not 30 <= self.job_timeout <= 3600 or not 1 <= self.max_calls <= 30:
            raise ValueError("任务时限应为30～3600秒，调用上限应为1～30")
        if not 0 <= self.max_repairs <= 4 or not 512 <= self.max_tokens <= 64000:
            raise ValueError("修复次数或输出长度不在支持范围内")
        if not 30 <= self.http_timeout <= 1200:
            raise ValueError("单请求HTTP超时须为30～1200秒")
        if self.effort not in {"low", "high", "max"}:
            raise ValueError("reasoning effort 须为 low/high/max")
        if self.review_effort not in {'','low','high','max'}:
            raise ValueError('review effort须为空（沿用全局）或low/high/max')
        if self.review_effort and self.api_style!='deepseek':raise ValueError('分阶段复核思考强度目前仅支持DeepSeek')
        if self.incremental_math_review and not self.compact_tools:
            raise ValueError("局部数学复核仅支持compact模式")
        if self.review_responses and (self.api_style != "deepseek" or not self.compact_tools):
            raise ValueError("Responses复核仅支持DeepSeek compact模式")
        if self.api_style not in {"deepseek", "glm"}:
            raise ValueError("AGENT_API_STYLE 仅支持 deepseek/glm")
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

