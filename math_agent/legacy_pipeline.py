from __future__ import annotations
import copy
import base64
import json
import time
import threading
from pathlib import Path
from .cache import Cache, key_for
from .contracts import Source, Candidate, Review, MathRepair, ReviewPreparation, check_source, check_candidate, check_review
from .prompts import messages
from .provider import DeepSeek
from .rendering import render, RenderError
from .storage import Store, write_json
from .tools import DEFINITIONS, dispatch
from .scene_tools import SceneTools, PREVIEW, INSPECT, compact_observation
from .contracts import Issue, Adjudication, exact_ids
from .evidence import build_ledger, read_ledger, validate_basis, record_hypothesis, READ_EVIDENCE, RECORD_HYPOTHESIS
from .image_tools import ImageTools, VIEW_REGION


class NeedsInput(ValueError):
    pass


def canonical_page(candidate, source, audit=None):
    """A single authoritative solution supplies displayed steps and answers."""
    from .parent_binding import bind_parent_roles
    candidate.spec,_=bind_parent_roles(candidate.spec,audit)
    candidate.spec["problem_text"] = source.problem_text
    for solution, tab in zip(candidate.solutions, candidate.spec.get("tabs", [])):
        tab["steps"] = [{"head": step.rule, "content": step.premise + "\n" + step.conclusion} for step in solution.steps]
        if solution.cases:
            tab["steps"].append({"head": "分类讨论", "content": "\n".join(solution.cases)})
        tab["steps"].append({"head": "定义域与边界检查", "content": solution.domain_and_boundaries})
        tab["conclusion"] = solution.result
    return candidate


def review_payload(candidate, records):
    """Remove duplicated presentation text and historical draft specs, not evidence."""
    data = candidate.model_dump()
    spec = data['spec']
    spec.pop('problem_text', None)  # Full authoritative source is supplied separately.
    for solution, tab in zip(data['solutions'], spec.get('tabs', [])):
        # canonical_page supplied these strings; solutions carries all their content.
        tab.pop('steps', None)
        tab.pop('conclusion', None)
    return data, compact_tool_evidence(records)


def compact_tool_evidence(records):
    evidence = copy.deepcopy(records)
    for record in evidence:
        if record.get('name') != 'preview_scene': continue
        try:
            args = json.loads(record.get('arguments', '{}'))
            if 'spec' in args:
                from .scene_tools import fingerprint
                args['draft_spec_sha256'] = fingerprint(args.pop('spec'))
            record['arguments'] = json.dumps(args,ensure_ascii=False)
        except (ValueError, TypeError):
            pass  # Preserve malformed evidence instead of silently dropping it.
    return evidence


def route(review):
    stages = {i.stage for i in review.issues}
    return "source" if "source" in stages else "math_repair" if "math" in stages else "diagram"


def failed_inspection_detail(result):
    failures = [{"tab":p["tab"], "parameter":p.get("parameter"), "check":c}
                for p in result.get("probes", []) for c in p.get("checks", [])
                if c.get("matches_expected") is False or c.get("error")]
    data = {"ok":result.get("ok"), "error":str(result.get("error", ""))[:400],
            "failed_checks":failures[:6], "failed_count":len(failures)}
    while len(json.dumps(data,ensure_ascii=False)) > 2200 and len(data["failed_checks"]) > 1:
        data["failed_checks"].pop()
    return json.dumps(data,ensure_ascii=False)[:2400]


class Runner:
    def __init__(self, settings, job_id, provider_factory=DeepSeek, cache_factory=Cache, renderer=render):
        self.settings, self.job_id = settings, job_id
        self.store = Store(settings.data_dir)
        self.directory = self.store.path(job_id)
        self.request = json.loads((self.directory / "request.json").read_text())
        self.records = []
        self.started = time.monotonic()
        self._audit_lock = threading.RLock()
        self.provider_factory = provider_factory
        self.question_plan_metadata = None
        self.pending_ocr = None
        self.shared_budget = None
        self.parallel_review_rounds = []
        if settings.parallel_questions or settings.parallel_review:
            from .parallel_budget import ParallelBudget
            self.shared_budget = ParallelBudget(settings.max_calls,
                deadline=self.started+settings.job_timeout, max_concurrency=2 if settings.parallel_review else settings.question_concurrency)
        self.provider = provider_factory(settings, self.audit)
        if self.shared_budget is not None:self.provider.shared_budget = self.shared_budget
        self.cache = cache_factory(settings, self.audit)
        self.renderer = renderer
        self.scenes = SceneTools(self.directory / "observations")
        self.observation_count = 0
        self.source_rechecked = False
        self.resume_state = None
        self.ledger = {"entries":{}}
        self.image_tools = ImageTools(self.directory, self.request["images"])
        self.review_candidate = None
        self.review_snapshot = None
        self.review_certificates = []
        self.math_review_receipts = []
        self.review_change_tabs = None
        self.active_review_delta = None
        from .deferred_review import PendingReviews
        self.deferred_reviews = PendingReviews()
        pending_path=self.directory/'deferred-reviews.json'
        if pending_path.exists():self.deferred_reviews.load(json.loads(pending_path.read_text()))
        import hashlib
        package=Path(__file__).resolve().parent
        write_json(self.directory/'runtime-manifest.json',{'package_root':str(package),
            'python_files':{str(p.relative_to(package)):hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in sorted(package.rglob('*.py'))}})

    def audit(self, kind, detail):
        with self._audit_lock:
            entry = {"kind": kind, **copy.deepcopy(detail)}
            if "seconds" in detail:entry["duration_seconds"]=detail["seconds"]
            entry["seconds"]=round(time.monotonic()-self.started, 3)
            self.records.append(entry)
            write_json(self.directory / "audit.json", self.records)
            self.store.event(self.job_id, detail.get("stage", kind), json.dumps(detail, ensure_ascii=False)[:1800])

    @property
    def model_calls(self):
        return self.shared_budget.calls if self.shared_budget is not None else self.provider.calls

    def ask(self, stage, payload=None, request_override=None):
        self.store.event(self.job_id, stage, {"source": "识别题设与图片来源", "source_review":"独立核对读题疑问", "solve": "解题与工具核验", "math_repair":"修正数学证明", "diagram": "修复构图与交互", "review": "对照原图独立复核", "review_plan":"集中收集复核证据", "adjudicate":"审查失败断言的题设依据", "reference_math":"整理参考解法", "math_review":"核验数学解答", "draw":"生成原生几何图", "draw_patch":"局部修图", "graph_review":"核验图文关系", "diagram_review":"复用已审数学，核验变化画板", "deferred_review":"核对修订后仍待处理的检查"}[stage])
        active_request = self.request if request_override is None else request_override
        if stage in {"source", "source_review"}:
            active_request = {**active_request,
                "images": [i for i in active_request["images"] if i["role"] == "question"],
                "ocr_evidence": [p for p in active_request.get("ocr_evidence", []) if p["role"] == "question"]}
        if stage in {"draw", "draw_patch", "graph_review"}:
            active_request = {**active_request, "images":[i for i in active_request["images"] if i["role"]=="question"]}
        image_definitions = [VIEW_REGION] if active_request["images"] else []
        definitions = image_definitions if stage in {"source", "source_review"} else DEFINITIONS + [READ_EVIDENCE] + image_definitions + (
            [INSPECT] if stage in {"review","review_plan","adjudicate","graph_review","diagram_review","deferred_review"} else [PREVIEW, RECORD_HYPOTHESIS])
        if stage in {"reference_math", "math_review", "math_repair"}:
            definitions = [d for d in DEFINITIONS if d['function']['name'] in {'algebra_batch','symbolic_geometry'}] + image_definitions + [READ_EVIDENCE]
        elif stage in {"draw", "draw_patch"}:
            definitions = [PREVIEW, READ_EVIDENCE] + image_definitions
        elif stage in {"graph_review","diagram_review","deferred_review"}:
            definitions = [INSPECT, READ_EVIDENCE] + image_definitions
        if stage=="diagram_review":
            from .review_reuse import query_tool
            definitions=[query_tool(payload['reuse_certificate']) if d['function']['name']=='inspect_scene' else d for d in definitions]
        if stage=="review":
            from .rotation_probe import TOOL as ROTATION_PROBE
            definitions=definitions+[ROTATION_PROBE]
        from .scene_tools import inspection_definition
        definitions=[inspection_definition(d,query_only=stage in {'adjudicate','review_plan','deferred_review'})
                     if d['function']['name']=='inspect_scene' else d for d in definitions]
        correction=(payload or {}).get('structured_output_correction',{})
        if correction.get('final_output_only'):
            definitions=[]
            self.audit('format_only_correction',{'stage':stage,'detail':'仅修正原编号覆盖，使用已附真实证据，不追加工具往返'})
        if self.settings.compact_tools and stage == "solve":
            definitions = [d for d in definitions if d['function']['name'] in
                {'algebra_batch', 'symbolic_geometry', 'view_image_region'}]
        if stage in {'review','graph_review'} and self.review_candidate is not None:
            from .evidence import assertion_basis_index
            payload={**payload,'assertion_basis_index':assertion_basis_index(self.ledger,self.review_candidate.solutions)}
        if stage in {'review','review_plan','adjudicate','graph_review','diagram_review','deferred_review'} and self.review_candidate is not None:
            from .jsxgraph.spec import POINTS
            payload={**payload,'inspection_point_index':{str(i):{'question_id':s.question_id,
                'point_ids':[o['id'] for o in self.review_candidate.spec['tabs'][i]['board']['objects'] if o['kind'] in POINTS]}
                for i,s in enumerate(self.review_candidate.solutions)}}
        solve_contract=None
        if stage=='solve' and self.settings.compact_tools:
            from .compact_output import output_schema
            solve_contract=payload.get('submission_patch_schema') or output_schema(payload['source'],require_basis=self.settings.parallel_questions)
        prompt = messages(stage, active_request, self.directory, payload, compact=self.settings.compact_tools,solve_schema=solve_contract)
        if stage in {'source','source_review'} and self.settings.native_constructions:
            from .native_constructions import INSTRUCTION
            prompt[0]['content']+='\n'+INSTRUCTION
        if stage=='source' and self.settings.parallel_questions:
            from .parallel_solve import SOURCE_PLAN_INSTRUCTION
            prompt[0]['content'] += '\n'+SOURCE_PLAN_INSTRUCTION
        if self.settings.compact_tools and stage == "solve":
            prompt[0]['content'] += "\n本阶段工具以实际声明为准：仅可批量数学计算与局部原图读取，不调用预览或重复证据读取。source已完整提供。构图只在最终小问对象中输出一次，不先写整套图形作为工具参数。提交后服务器会实际渲染JSXGraph并独立复核；不能宣称尚未执行的浏览器检查已通过。采用参考解法并补齐必要依据，避免反复探索多个方案；关键计算尽量一次批量核验。"
        if self.settings.review_reserve_seconds and stage in {"solve","review","math_repair","diagram"}:
            prompt[0]['content'] += "\n本任务按阶段预留时间：尽快选择一种可核验的初中解法，不反复探索替代方法。关键计算合并一批，坐标和代数使用精确根式，避免浮点误差触发重算。完整证明优先，精简重复措辞；不得省略依据、边界或未决异议来凑预算。"
        if stage in {"review", "review_plan", "adjudicate", "graph_review", "diagram_review", "deferred_review"}:
            # Trusted renderer artifacts, not model-specified paths. Text evidence covers every tab.
            shots=sorted((self.directory / "output").glob("tab-*.png"))
            if stage=="diagram_review":
                allowed={f"tab-{i:02d}" for i in payload['reuse_certificate']['changed_tabs']}
                shots=[p for p in shots if p.stem in allowed]
            for path in (shots if stage=='review' and payload.get('parallel_review_scope') else shots[:3]):
                prompt[-1]["content"].extend([
                    {"type":"text","text":"实际JSXGraph截图："+path.stem},
                    {"type":"image_url","image_url":{"url":"data:image/png;base64,"+base64.b64encode(path.read_bytes()).decode()}}])
        if stage == "review_plan": definitions=[]
        tool_dispatch=lambda name,args:self.dispatch_stage_tool(stage,active_request,name,args)
        schema_args = {}
        if self.settings.solve_responses and stage in {'solve','diagram'}:
            from .compact_output import output_schema,native_schema
            from .diagram_patch import DiagramPatch
            schema_args['output_schema']=(native_schema(DiagramPatch.model_json_schema()) if stage=='diagram' else
                solve_contract or output_schema(payload['source']))
            if stage=='diagram' and payload.get('allowed_tabs') is not None:
                schema_args['output_schema']['$defs']['Edit']['properties']['tab']['enum']=payload['allowed_tabs']
        if stage in {"review","deferred_review","math_repair"} and self.settings.review_responses:
            from .compact_review import output_schema
            if stage == "review":schema_args["output_schema"] = output_schema(payload.get("review_output_source", payload["source"]),payload.get('literal_passages'),flat=payload.get('flat_review',False))
            elif stage == "math_repair":
                from .math_patch import output_schema as patch_schema
                schema_args["output_schema"] = patch_schema(payload["source"])
            else:
                from .deferred_review import DeferredVerdict
                schema_args["output_schema"] = DeferredVerdict.model_json_schema()
        from .submission import receive
        value = receive(self.provider,stage,prompt,self.audit,tools=definitions,dispatch=tool_dispatch,**schema_args)
        write_json(self.directory / f"model-{self.provider.calls:02d}-{stage}.json", value)
        return value

    def dispatch_stage_tool(self, stage, active_request, name, args):
        if name == "view_image_region":
            if args.get("image_id") not in {i["id"] for i in active_request["images"]}:
                raise ValueError("本阶段不能查看这张图片")
            return self.image_tools.view(**args)
        if name == "probe_rotation_rule":
            if stage!="review" or self.review_candidate is None:
                raise ValueError("旋转前提取证仅供当前独立数学复核使用")
            if self.scenes.count>=12:raise ValueError("浏览器观察工具次数达到上限")
            self.scenes.count+=1
            from .rotation_probe import evaluate
            return evaluate(self.review_candidate,args,self.directory/'rotation-probes'/f'probe-{self.scenes.count:02d}')
        if name == "read_evidence": return read_ledger(self.ledger,args["ids"])
        if name == "record_hypothesis":
            result=record_hypothesis(self.ledger,**args)
            write_json(self.directory / "evidence-ledger.json", self.ledger)
            return result
        if name in {"preview_scene", "inspect_scene"}:
            if (stage in {"review","review_plan","adjudicate","graph_review","diagram_review","deferred_review"}) != (name == "inspect_scene"):
                raise ValueError("当前阶段不能调用此图形工具")
            if name == "inspect_scene":
                from .inspection_recovery import schema_failure,reference_failure
                failure=schema_failure(args,self.scenes.current)
                if failure:return failure
                failure=reference_failure(args,self.scenes.current)
                if failure:return failure
                if stage=="diagram_review" and self.review_change_tabs is not None:
                    if any(p['tab'] not in self.review_change_tabs for p in args['probes']):
                        raise ValueError("局部复核只能查询实际变化画板："+str(self.review_change_tabs))
                invalid_checks = []
                errors = []
                from .inspection_recovery import target
                from .scene_tools import fingerprint
                from .rendering import strict_spec
                if stage in {"adjudicate","review_plan","diagram_review","deferred_review"}:
                    forbidden = [target(p,{k:v for k,v in c.items() if k!="expected"},self.scenes.current)
                        for p in args.get("probes",[]) for c in p.get("checks",[]) if "expected" in c]
                    if forbidden:
                        return {"ok":False,"error":"本阶段只能查询：删除expected后，按原tab、参数、关系和点序重做以下查询；不得跳过。",
                            "execution_failure":{"kind":"query_only","spec_sha256":fingerprint(strict_spec(self.scenes.current)),"targets":forbidden}}
                for probe in args.get("probes", []):
                    for check in probe.get("checks", []):
                        if "expected" not in check: continue
                        if stage in {"adjudicate","review_plan","diagram_review","deferred_review"}: raise ValueError("本阶段只做查询，省略expected；根据真实观察和题设判断原断言是否合理")
                        tab=probe.get("tab")
                        if type(tab) is not int or not self.review_candidate or not 0 <= tab < len(self.review_candidate.solutions):
                            raise ValueError("观察小问不存在")
                        try:
                            validate_basis(self.ledger,check.get("basis_ids",[]),self.review_candidate.solutions[tab].question_id)
                            if not check.get("rationale"): raise ValueError("请解释依据为什么要求这个expected，或省略expected作查询")
                        except ValueError as exc:
                            invalid_checks.append(target(probe, check, self.scenes.current)); errors.append(str(exc))
                if invalid_checks:
                    from .evidence import assertion_basis_index
                    index=assertion_basis_index(self.ledger,self.review_candidate.solutions)
                    return {"ok":False, "error":"；".join(errors), "execution_failure":{
                        "kind":"basis_validation", "spec_sha256":fingerprint(strict_spec(self.scenes.current)),
                        "targets":invalid_checks},
                        "available_assertion_basis":{str(t['tab']):index[str(t['tab'])] for t in invalid_checks},
                        "recovery_hint":"所列编号仅满足本画板作用域，不证明断言成立。须核对内容与rationale；依据不足时省略expected作查询，原失败仍按既有规则逐条处理，不得删掉或声称已通过。"}
            result = self.scenes.dispatch(name,args)
            if name == "inspect_scene" and result.get("ok") is False and not result.get("execution_failure"):
                failed = [target(p,c) for p in result.get("probes",[]) for c in p.get("checks",[]) if "error" in c]
                result["execution_failure"] = {"kind":"browser_execution", "spec_sha256":fingerprint(strict_spec(self.scenes.current)),
                    "targets":failed or [target(p,c,self.scenes.current) for p in args.get("probes",[]) for c in p.get("checks",[])]}
                from .inspection_recovery import partition_pending
                _, malformed = partition_pending([{"target":t} for t in result["execution_failure"]["targets"]],self.scenes.current)
                if malformed:
                    result["invalid_queries"] = malformed
                    result["recovery_hint"] = "这些关系的点数不合法，不能原样重做。需要观察时改用合法点数；最终review保留真实数学异议。原无效请求会交独立裁决，当前并未解除或视作通过。"
            return result
        return dispatch(name,args)

    def validated(self, stage, payload, validate, request_override=None):
        """Correct invalid structured output once, within the existing paid-call budget.

        Transport errors and truncation are deliberately outside the catch. Invalid
        evidence is never silently discarded, and only validated results advance.
        """
        context = dict(payload)
        for attempt in range(2):
            raw = self.ask(stage, context, request_override=request_override)
            try:
                return validate(raw)
            except ValueError as exc:
                self.audit("contract_error", {"stage": stage, "attempt": attempt + 1,
                    "detail": str(exc)[:4000]})
                if attempt: raise
                context = {**payload, "structured_output_correction": {
                    "error": str(exc)[:24000], "rejected_output": raw,
                    "instruction": ("这是数学局部纠错输出：仅返回updates对象，按Q编号提交需更改小问的reason与完整proof；无关小问由服务端保留，不能返回solutions/spec/tabs或改题设。" if stage == "math_repair" and self.settings.compact_tools else "这是数学纠错输出：仅返回完整solutions，不返回spec或tabs；保留真实异议，不能改题设或删小问。" if stage == "math_repair" else "这是读题输出纠错：只修正本阶段source或source_review JSON，对照原题核对文字、条件和引用。不要输出solutions或tabs；保留真实疑问，不能为通过校验修改题意。" if stage in {"source","source_review"} else "这是复核/裁决输出纠错：修正完整的本阶段JSON，核对verdict与逐项核验、issues的一致性以及裁决编号/作用域。不要输出solutions或tabs；明确保留真实异议、未知项，不要为通过格式而编造或删除问题。"
                        if stage in {"review","review_plan","graph_review","math_review","adjudicate","diagram_review","deferred_review"} else
                        "按系统声明的DiagramPatch提交局部edits，不返回数学解答或整页字段。" if self.settings.compact_tools and stage == "diagram" else
                        "按系统声明的小问对象契约修正完整JSON；每问恰好一次，保留证明与真实异议。" if self.settings.compact_tools and stage == "solve" else
                        "修正完整JSON并重新提交。严格区分条件编号和小问编号，按source的小问数量与顺序组织solutions和tabs。不得删掉未决读题问题、异议或偷偷改变题设以通过校验。")}}
                if stage in {"review","diagram_review","graph_review","adjudicate","review_plan","math_review","deferred_review"}:
                    fresh_evidence=compact_tool_evidence([r for r in self.records if r['kind']=='tool'][-24:])
                    context['tool_evidence']=fresh_evidence
                    from .compact_review import ReviewShapeError
                    if isinstance(exc,ReviewShapeError):
                        context['structured_output_correction']['final_output_only']=True
                        context['structured_output_correction']['instruction']+=' 这是纯编号覆盖纠正，本轮只输出修正后的完整JSON，不调用工具；若已有证据不足，明确保留unknown/异议，不能为凑编号编造通过。'
                    context['structured_output_correction']['instruction']+=' 已附上刚才真实执行的工具结果，保留这些证据；复用成功观察，只修正出错的格式/编号覆盖，不为格式纠正重复解题或重跑同一成功查询。若确有失败或未执行的必需检查才补做；真实异议和未知项仍须保留。'
        raise AssertionError("unreachable")

    def extract(self, feedback=None):
        if "ocr_evidence" not in self.request:
            from .ocr import recognize
            if self.settings.overlap_ocr and self.settings.ocr_url and self.request['images']:
                from .ocr_overlap import PendingOCR
                self.pending_ocr=PendingOCR(self.settings,self.request,self.directory,self.audit,
                    self.started+self.settings.job_timeout,recognize)
                self.request['ocr_evidence']=[]
                self.request['_ocr_overlap']=True
            else:
                evidence = recognize(self.settings, self.request, self.directory, self.audit)
                self.request["ocr_evidence"] = evidence
                if evidence: write_json(self.directory / "ocr.json", evidence)
        def validate(raw):
            from .native_constructions import require_output
            require_output(raw.get('source') if self.settings.parallel_questions and isinstance(raw,dict) else raw,
                           self.settings.native_constructions)
            if self.settings.parallel_questions:
                if not isinstance(raw,dict) or set(raw)!={'source','parallel_plan'}:
                    raise ValueError('小问实验读题输出须为source与parallel_plan的完整封装')
                value = Source.model_validate(raw['source'])
                from .question_plan import build_plan
                build_plan(value,raw['parallel_plan'])
                self.question_plan_metadata=copy.deepcopy(raw['parallel_plan'])
                write_json(self.directory/'question-plan-metadata.json',self.question_plan_metadata)
            else:value = Source.model_validate(raw)
            check_source(value, self.request["images"])
            return value
        source = self.validated("source", {"feedback": feedback or []}, validate)
        if source.uncertainties:self.complete_ocr()
        if source.uncertainties and self.request.get("ocr_evidence") and not self.source_rechecked:
            self.question_plan_metadata = None
            self.source_rechecked = True
            write_json(self.directory / "source-before-reread.json", source.model_dump())
            self.audit("source_reread", {"stage":"source", "reason":"OCR辅助读题仍有未决问题；仅用原题重新核对一次，不携带OCR或参考答案", "uncertainties":source.uncertainties})
            original = {**self.request, "ocr_evidence":[], "images":[i for i in self.request["images"] if i["role"] == "question"]}
            from .source_review import check_reread, apply_reread
            def validate_reread(raw):
                from .native_constructions import require_output
                require_output(raw.get('source') if isinstance(raw,dict) else None,self.settings.native_constructions)
                return check_reread(raw,issues,original['images'])
            issues = [{"id":f"U{i+1}","text":text} for i,text in enumerate(source.uncertainties)]
            decision = self.validated("source_review", {"previous_source":source.model_dump(),
                "issues":issues, "feedback":feedback or []},
                validate_reread, request_override=original)
            write_json(self.directory / "source-question-review.json", decision.model_dump())
            self.audit("source_question_review", {"stage":"source_review", "decisions":[d.model_dump() for d in decision.decisions]})
            source = apply_reread(decision,issues)
        write_json(self.directory / "source.json", source.model_dump())
        if source.uncertainties: raise NeedsInput("；".join(source.uncertainties))
        self.save_ledger(source)
        return source

    def complete_ocr(self):
        if self.pending_ocr is not None:
            evidence=self.pending_ocr.result()
            self.request['ocr_evidence']=evidence
            if evidence:write_json(self.directory/'ocr.json',evidence)

    def save_ledger(self, source, candidate=None):
        self.ledger = build_ledger(source,candidate)
        write_json(self.directory / "evidence-ledger.json", self.ledger)
        n=len(list(self.directory.glob("evidence-version-*.json")))+1
        write_json(self.directory / f"evidence-version-{n:02d}.json",self.ledger)
        self.audit("evidence", {"entries":len(self.ledger["entries"]), "version":n,
            "detail":"原题条件、目标和待核验推导分开记录；引用不等于已经证明"})

    def validate_parallel_candidate(self,source,candidate):
        if self.settings.parallel_questions:
            from .question_plan import build_plan,validate_basis
            validate_basis(source,build_plan(source,self.question_plan_metadata),candidate.solutions)

    def solve(self, source, feedback=None, previous=None, stage="solve", _whole=False, allowed_tabs=None):
        if self.settings.parallel_questions and stage=='solve' and not _whole:
            from .parallel_solve import solve
            return solve(self,source,feedback)
        if self.settings.compact_tools and stage=='solve':
            return self.solve_compact(source,feedback)
        def validate(raw):
            from .compact_output import unpack
            if self.settings.compact_tools and stage == "diagram":
                from .diagram_patch import apply_patch
                candidate = apply_patch(raw, previous, self.audit,allowed_tabs=allowed_tabs)
            else:
                candidate = unpack(raw, source) if self.settings.compact_tools and stage == "solve" else Candidate.model_validate(raw)
            # This is a mathematical boundary, not a format issue to normalize away.
            if stage == "diagram" and candidate.solutions != previous.solutions:
                raise RuntimeError("构图修复试图改写数学解答，已拦截；需要回到解题阶段")
            canonical_page(candidate, source, self.audit)
            check_candidate(candidate, source)
            if stage=='diagram':
                from .submission import inspect,SubmissionError
                issues=inspect(candidate,source)
                if issues:raise SubmissionError(issues,candidate)
            return candidate
        candidate = self.validated(stage, {"source": source.model_dump(), "feedback": feedback or [],
            "previous": previous.model_dump() if previous else None,
            **({'allowed_tabs':sorted(allowed_tabs)} if allowed_tabs is not None else {})}, validate)
        self.save_ledger(source,candidate)
        return candidate

    def solve_compact(self,source,feedback=None):
        payload={'source':source.model_dump(),'feedback':feedback or []}
        raw=self.ask('solve',payload)
        from .question_plan import build_plan
        plan=build_plan(source,self.question_plan_metadata) if self.settings.parallel_questions else None
        return self.finish_compact(raw,source,feedback,require_basis=self.settings.parallel_questions,dependency_plan=plan)

    def finish_compact(self,raw,source,feedback=None,require_basis=False,dependency_plan=None):
        """One common submission/recovery gate for whole and merged branch output."""
        from .submission import decode,SubmissionError,patch_schema,apply_patch,allowed_fields
        def decode_submission(value):return decode(value,source,self.audit,require_basis=require_basis,dependency_plan=dependency_plan)
        payload={'source':source.model_dump(),'feedback':feedback or []}
        try:
            candidate=decode_submission(raw)
        except ValueError as exc:
            self.audit('contract_error',{'stage':'solve','attempt':1,'detail':str(exc)[:12000]})
            issues=exc.issues if isinstance(exc,SubmissionError) else None
            if issues:
                try:allowed=allowed_fields(issues)
                except ValueError:allowed=None
            else:allowed=None
            if allowed and exc.candidate is not None and all(i['stage']=='diagram' for i in issues):
                # A drawing-only correction has no permission to replace proof.
                from .diagram_patch import apply_patch as diagram_patch
                ids=[q.id for q in source.questions]
                tabs={ids.index(q) for q in allowed}
                correction=self.ask('diagram',{'source':source.model_dump(),'previous':exc.candidate.model_dump(),
                    'feedback':issues,'allowed_tabs':sorted(tabs)})
                candidate=diagram_patch(correction,exc.candidate,self.audit,allowed_tabs=tabs)
                canonical_page(candidate,source,self.audit)
                from .submission import inspect
                remaining=inspect(candidate,source,require_basis=require_basis,dependency_plan=dependency_plan)
                if remaining:raise SubmissionError(remaining,candidate)
            elif allowed:
                correction=self.ask('solve',{**payload,'submission_patch_schema':patch_schema(issues,source.model_dump(),require_basis=require_basis),
                    'structured_output_correction':{'issues':issues,'rejected_output':raw,
                        'instruction':'只返回updates，按错误清单修改指定小问和字段；保留真实未知及异议，不改题设，不删除未授权内容。'}})
                candidate=decode_submission(apply_patch(correction,raw,issues))
            else:
                # Malformed outer coverage cannot safely identify a partial scope.
                correction=self.ask('solve',{**payload,'structured_output_correction':{
                    'error':str(exc)[:12000],'rejected_output':raw,
                    'instruction':'外层编号无法安全绑定；修正完整小问对象，保留原题和全部真实未知，不得删问。'}})
                candidate=decode_submission(correction)
            self.audit('submission_correction',{'status':'local_contract_pass',
                'scope':{q:sorted(fields) for q,fields in (allowed or {}).items()},
                'detail':'只纠正已报告范围；仍须真实渲染与独立整题复核。最多一次输出纠正，未增加调用预算。'})
        self.save_ledger(source,candidate)
        return candidate

    def repair_math(self, source, previous, feedback):
        def validate(raw):
            if self.settings.compact_tools:
                from .math_patch import apply
                candidate,patch=apply(raw,previous,source)
                self.audit('math_patch',{'questions':list(patch.updates),
                    'reasons':{k:v.reason for k,v in patch.updates.items()},
                    'detail':'仅应用声明小问的证明；其余证明和全部几何对象原样保留，仍需重新独立复核'})
            else:
                repair = MathRepair.model_validate(raw)
                candidate = Candidate(solutions=repair.solutions, spec=copy.deepcopy(previous.spec))
            canonical_page(candidate, source, self.audit)
            check_candidate(candidate, source)
            self.validate_parallel_candidate(source,candidate)
            return candidate
        candidate = self.validated("math_repair", {
            "source":source.model_dump(), "previous":{"solutions":[s.model_dump() for s in previous.solutions]},
            "feedback":feedback}, validate)
        from .deferred_review import content_binding
        prior=getattr(self,'last_review_context',None)
        self.repair_review_context=None
        if prior and prior['binding']==content_binding(source,previous,self.request):
            unchanged=[a.question_id for a,b in zip(previous.solutions,candidate.solutions) if a==b]
            self.repair_review_context={'current_binding':content_binding(source,candidate,self.request),
                'previous_review':prior['review'],'unchanged_questions':unchanged,
                'changed_questions':[q.id for q in source.questions if q.id not in unchanged],
                'detail':'服务端逐字数据比较；题设/原图/图形不变。原复核只是模型判断，不是形式化证明，新发现的真实问题仍须报告。'}
        self.pending_math_objections = copy.deepcopy(feedback)
        self.save_ledger(source,candidate)
        self.audit("math_repair", {"detail":"仅更新数学解答及派生讲解文字；原图保留，必须重新观察并独立复核，旧复核不复用"})
        return candidate

    def observe(self, candidate):
        self.observation_count += 1
        self.scenes.current = copy.deepcopy(candidate.spec)
        output = self.directory / "output"
        # Failed attempts must not leave stale successful evidence behind.
        if output.exists():
            for name in ["export-check.json", "observation-failure.json"]:
                (output/name).unlink(missing_ok=True)
            for path in output.glob("tab-*.png"): path.unlink()
        from .explanation_coverage import require as require_explanation
        require_explanation(candidate)
        try:
            self.renderer(candidate.spec, output)
        except RenderError:
            failed = output / "observation-failure.json"
            if failed.exists():
                self.audit("observation", {"status":"fail", "evidence":json.loads(failed.read_text())})
            raise
        report_path = output / "export-check.json"
        if not report_path.is_file():
            raise RenderError("缺少真实浏览器观察报告，不能进入复核")
        report = json.loads(report_path.read_text())
        if report.get("status") != "pass": raise RenderError("浏览器观察未通过")
        observation = compact_observation(report, candidate.spec)
        write_json(self.directory / f"observation-{self.observation_count:02d}.json", observation)
        self.audit("observation", observation)
        return observation

    def review(self, source, candidate, observation, stage="review"):
        self.complete_ocr()
        from .review_reuse import Snapshot
        delta=None
        if self.settings.incremental_review and stage=="review" and self.review_snapshot:
            delta=self.review_snapshot.delta(source,candidate,self.request,self.directory)
        if self.settings.incremental_review and stage=="review" and delta is None:
            if self.review_snapshot:self.audit("review_reuse_invalidated",{'detail':'内容或作用域不匹配；重新完整复核，不沿用旧证书链'})
            self.review_snapshot=None
            self.review_certificates=[]
        actual_stage="diagram_review" if delta else stage
        if delta:
            self.audit("review_reuse", {"certificate":delta['certificate'],"changed_tabs":delta['changed_tabs'],
                "detail":"原图/题设/数学一致且上次全部数学项pass；独立复核全部变化画板及原异议"})
        math_scope=None
        if self.settings.incremental_math_review and stage=="review" and not delta:
            from .math_review_scope import plan
            math_scope=plan(getattr(self,'math_review_baseline',None),source,candidate,self.request,self.directory)
            if math_scope:
                self.audit('math_review_scope',{'sha256':math_scope['sha256'],
                    'question_ids':math_scope['question_ids'],'reused_questions':math_scope['reused_questions'],
                    'detail':'串行复核修改问及依赖；全部条件、原图、画板、真实工具与暂存裁决保持'})
        value=self._review(source,candidate,observation,actual_stage,delta,math_scope)
        substantive=any(i.stage in {'source','math'} for i in value.issues)
        if self.deferred_reviews.pending and value.verdict!='needs_input' and not substantive:
            value=self.settle_deferred(source,candidate,observation,value)
        if self.settings.incremental_review and stage=="review":
            try:
                self.review_snapshot=Snapshot(source,candidate,value,observation,self.request,self.directory)
                record={'sha256':self.review_snapshot.certificate,'binding':self.review_snapshot.binding,'review':value.model_dump()}
                self.review_certificates.append(record)
                write_json(self.directory/'review-certificates.json',self.review_certificates)
            except ValueError:
                self.review_snapshot=None
        from .deferred_review import content_binding
        if self.settings.incremental_math_review and stage=='review':
            from .math_review_scope import capture
            self.math_review_baseline=capture(source,candidate,value,self.request,self.directory)
        self.last_review_context={'binding':content_binding(source,candidate,self.request),'review':value.model_dump()}
        return value

    def _review(self, source, candidate, observation, stage="review", delta=None, math_scope=None):
        self.review_change_tabs=delta["changed_tabs"] if delta else None
        self.active_review_delta=delta
        self.scenes.current = copy.deepcopy(candidate.spec)
        self.review_candidate = candidate
        self.save_ledger(source,candidate)
        evidence = [r for r in self.records if r["kind"] == "tool"][-24:]
        before = len(self.records)
        preparation = None
        from .proof_guards import collect as collect_proof_guards
        guards=collect_proof_guards(source,candidate,self.directory/'proof-guards')
        write_json(self.directory/'proof-guards.json',guards)
        if guards['observations'] or guards['unobserved']:
            self.audit('proof_case_observation',guards)
        if self.settings.review_plan and stage == "review":
            def validate_preparation(raw):
                value=ReviewPreparation.model_validate(raw)
                exact_ids(value.covered_questions,[q.id for q in source.questions],"复核取证小问")
                from jsonschema import validate as schema_validate, ValidationError
                def validate_schema(instance,schema):
                    try:schema_validate(instance,schema)
                    except ValidationError as exc:raise ValueError(exc.message) from exc
                from .scene_tools import PROBES
                if value.probes:validate_schema(value.probes,PROBES)
                if any('expected' in c for p in value.probes for c in p['checks']):
                    raise ValueError("取证计划只查询关系，不能填写expected")
                if value.algebra_operations:
                    algebra=next(d for d in DEFINITIONS if d['function']['name']=='algebra_batch')
                    validate_schema({'operations':value.algebra_operations},algebra['function']['parameters'])
                return value
            compact, _ = review_payload(candidate,[])
            preparation=self.validated("review_plan",{"source":source.model_dump(),"candidate":compact,
                "browser_observation":observation,"repair_objections":getattr(self,"pending_math_objections",[])},validate_preparation).model_dump()
            write_json(self.directory/f"review-preparation-{self.observation_count:02d}.json",preparation)
            for name,args in ([('inspect_scene',{'probes':preparation['probes']})] if preparation['probes'] else []) + ([('algebra_batch',{'operations':preparation['algebra_operations']})] if preparation['algebra_operations'] else []):
                try: result=self.dispatch_stage_tool('review_plan',self.request,name,args)
                except (ValueError,TypeError,KeyError) as exc: result={'ok':False,'error':str(exc)[:800]}
                self.audit('tool',{'stage':'review_plan','name':name,'arguments':json.dumps(args,ensure_ascii=False),'result':result})
            evidence=[r for r in self.records if r["kind"]=="tool"][-24:]
        scope_raw = {}
        def validate(raw):
            from .compact_review import unpack as unpack_review
            from .math_review_scope import merge as merge_math_scope
            review = (merge_math_scope(raw,math_scope,source) if math_scope else
                self.review_snapshot.merge(raw,delta,source) if delta else
                unpack_review(raw,source) if stage=="review" and self.settings.compact_tools else Review.model_validate(raw))
            if review.verdict == "pass" and review.issues:
                review.verdict = "revise"
                self.audit("review_consistency", {"stage":"review", "detail":"保留全部异议并降级为修复，不放行"})
            check_review(review, source)
            # A final JSON must also settle the actual tool work. Feed exact pending
            # obligations back through the existing bounded correction, not a new retry loop.
            from .inspection_recovery import reconcile, partition_pending
            from .scene_tools import fingerprint
            from .rendering import strict_spec
            inspections=[r for r in self.records[before:] if r["kind"]=="tool" and r.get("name")=="inspect_scene"]
            pending=reconcile(inspections,fingerprint(strict_spec(candidate.spec)))["unresolved"]
            pending, malformed = partition_pending(pending,candidate.spec)
            if pending:
                raise ValueError("还有真实图形工具查询未有效重做。保持同一图、tab、parameter、relation、points顺序和expected，纠正basis_ids后调用inspect_scene，不得删除检查或仅声称通过。待办："+json.dumps(pending,ensure_ascii=False))
            scope_raw["value"] = copy.deepcopy(raw)
            return review
        compact_candidate, evidence = review_payload(candidate, evidence)
        from .proof_dependencies import review_dependencies
        repair_context=getattr(self,'repair_review_context',None)
        if repair_context:
            from .deferred_review import content_binding
            if repair_context['current_binding']!=content_binding(source,candidate,self.request):repair_context=None
        if math_scope:
            compact_candidate['solutions']=[s for s in compact_candidate['solutions']
                if s['question_id'] in math_scope['context_questions']]
        context = {"source": source.model_dump(), "candidate": compact_candidate,
            "review_output_source":math_scope['output_source'] if math_scope else source.model_dump(),
            "math_review_scope":{k:math_scope[k] for k in ('sha256','question_ids','reused_questions','context_questions')} if math_scope else None,
            "repair_context":repair_context, "proof_guard_observations":guards,
            "proof_dependencies":review_dependencies(source,candidate.solutions),
            "preparation":preparation,
            "repair_objections":delta["objections"] if delta else getattr(self,"pending_math_objections",[]),
            "reuse_certificate":delta,
            "math_certificate":getattr(self,"math_certificate",None) if stage=="graph_review" else None,
            "tool_evidence": evidence, "browser_observation": observation}
        if self.settings.parallel_review and stage=='review':
            from .parallel_review import generate
            # Each child corrects its own malformed output/unfinished tools.
            # Never restart all paid reviews on a merge/transport failure.
            review = validate(generate(self,source,candidate,context))
        else:
            review = self.validated(stage,context,validate)
        if math_scope:
            from .math_review_scope import receipt
            self.math_review_receipts.append(receipt(math_scope,scope_raw['value'],review))
            write_json(self.directory/'math-review-receipts.json',self.math_review_receipts)
        inspections = [r for r in self.records[before:] if r["kind"] == "tool" and r.get("name") == "inspect_scene"]
        observations = [r["result"] for r in inspections]
        from .inspection_recovery import reconcile, partition_pending
        from .scene_tools import fingerprint
        from .rendering import strict_spec
        recovery = reconcile(inspections, fingerprint(strict_spec(candidate.spec)))
        self.audit("inspection_recovery", recovery)
        blocked, malformed = partition_pending(recovery["unresolved"],candidate.spec)
        if blocked:
            raise ValueError("真实图形工具执行失败，仍有未完成有效重做的检查，不能发布")
        if malformed:
            self.audit("inspection_referral", {"stage":stage,"pending":malformed,"detail":"无效查询参数或断言依据交独立裁决；此记录不是解除或通过"})
        failures=[]
        for item in malformed:
            t=item['target']
            failures.append({'id':f'A{len(failures)+1}', 'kind':'invalid_query',
                'tab':t['tab'], 'parameter':t['parameter'],
                'check':{k:t[k] for k in ('relation','points','expected')},
                'required_point_count':item['required_point_count'],
                'referral_reason':item['referral_reason'], 'original_error':item.get('error'),
                **{k:item[k] for k in ('invalid_point_ids','available_point_ids','permitted_parameter_range') if k in item},
                'detail':'未得到有合法依据的有效检查；必须独立裁决，不能原样重做或自动撤销。'})
        for result in observations:
            for probe in result.get("probes", []):
                for check in probe.get("checks", []):
                    if check.get("matches_expected") is False and "error" not in check:
                        failures.append({"id":f"A{len(failures)+1}","tab":probe["tab"],
                            "parameter":probe.get("parameter"),"check":check})
        if any(r.get("ok") is True and r.get("assertions_pass") is False for r in observations) and not failures:
            raise ValueError("真实图形工具返回断言失败但没有明细，不能发布")
        rotation_results=[r.get('result',{}) for r in self.records[before:] if r['kind']=='tool' and r.get('name')=='probe_rotation_rule']
        for result in rotation_results:
            # The hypothesis is never certified as the default scene. Only
            # actually observed, bound precondition failures enter the gate.
            for failure in result.get('counterexamples',[]):
                failures.append({'id':f'A{len(failures)+1}',**failure})
            for missed in result.get('unobserved',[]):
                args=result['request']
                for points in args['triangles']:
                    failures.append({'id':f'A{len(failures)+1}','kind':'proof_guard','tab':args['tab'],'parameter':None,
                        'check':{'relation':'collinear','points':points,'expected':False,'holds':None,
                                 'matches_expected':None,'observed':{}},
                        'basis_ids':[result['step_id']],'rule':result['rule'],
                        'case_evidence':{'question_id':result['question_id'],'angle_degrees':missed['angle'],
                            'rotation_ids':args['rotation_ids'],'original_spec_sha256':result['original_spec_sha256'],
                            'unobserved':missed},
                        'detail':'Agent请求的旋转前提取证未成功，没有共线或非共线结论。须独立判断原假设是否适用、证明前提是否已另证；不能以未返回反例冒充通过，证据不足保持unresolved。'})
        for failure in guards['failures']:
            failures.append({'id':f'A{len(failures)+1}',**failure})
        diagram_objections = [{"id":f"R{i+1}","kind":"review_issue","issue":issue.model_dump()}
            for i,issue in enumerate(review.issues) if issue.stage == "diagram"]
        if (failures or diagram_objections) and review.verdict=='revise' and any(i.stage in {'source','math'} for i in review.issues):
            self.deferred_reviews.add(source,candidate,observation,self.request,failures+diagram_objections,review)
            write_json(self.directory/'deferred-reviews.json',self.deferred_reviews.dump())
            self.review_snapshot=None
            self.review_certificates=[]
            self.audit('review_deferred',{'stage':stage,'pending':len(self.deferred_reviews.pending),
                'detail':'实质读题/数学异议先回对应Agent修正；其余检查连同原版本完整暂存，发布前必须逐条裁决'})
            return review
        if failures or diagram_objections:
            decisions = self.adjudicate(source,candidate,observation,failures + diagram_objections,
                original_review=review if diagram_objections else None)
            if diagram_objections:
                # All textual objections are independently accounted for, never implicitly erased
                # just because one numeric expectation was invalid.
                review = decisions.reviewed
                if any(d.verdict == "unresolved" for d in decisions.decisions):
                    raise NeedsInput("构图异议裁决仍有未决项")
                return review
            by_assertion={f['id']:f for f in failures}
            for decision in decisions.decisions:
                if decision.verdict == "unresolved":
                    raise NeedsInput("断言依据仍无法确定："+decision.reason)
                if decision.verdict == "real_issue":
                    review.verdict="revise"
                    review.issues.append(decision.issue)
                    failure=by_assertion[decision.assertion_id]
                    if 'tab' in failure:
                        from .contracts import Check
                        qid=candidate.solutions[failure['tab']].question_id
                        review.questions=[Check(id=q.id,verdict='issue',evidence=decision.issue.detail)
                            if q.id==qid and q.verdict!='unknown' else q for q in review.questions]
            # Existing reviewer objections are never deleted, even when a tool expectation was invalid.
            check_review(review,source)
        return review

    def settle_deferred(self, source, candidate, observation, review):
        """Fresh current review precedes an independent, explicitly version-bound resolution."""
        payload=self.deferred_reviews.payload(source,candidate,self.request)
        self.scenes.current=copy.deepcopy(candidate.spec)
        self.review_candidate=candidate
        self.save_ledger(source,candidate)
        self.review_change_tabs=None
        self.active_review_delta=None
        before=len(self.records)
        def validate(raw):
            result=self.deferred_reviews.validate(raw,payload,source,candidate,self.ledger)
            from .inspection_recovery import reconcile
            from .scene_tools import fingerprint
            from .rendering import strict_spec
            checks=[r for r in self.records[before:] if r['kind']=='tool' and r.get('name')=='inspect_scene']
            if reconcile(checks,fingerprint(strict_spec(candidate.spec)))['unresolved']:
                raise ValueError('暂存裁决中的工具失败尚未有效重做，不能解除待审检查')
            return result
        result=self.validated('deferred_review', {**payload,'source':source.model_dump(),
            'candidate':candidate.model_dump(),'browser_observation':observation,
            'current_review':review.model_dump(),'evidence_ledger':self.ledger,
            'tool_evidence':compact_tool_evidence([r for r in self.records if r['kind']=='tool'][-24:])},validate)
        if any(r.verdict=='unresolved' for r in result.resolutions):
            write_json(self.directory/'deferred-last-unresolved.json',result.model_dump())
            raise NeedsInput('修订后仍有暂存检查未决；原记录保留，不能发布')
        self.deferred_reviews.commit(result,payload)
        write_json(self.directory/'deferred-reviews.json',self.deferred_reviews.dump())
        for decision in result.resolutions:
            if decision.verdict=='real_issue':
                review.issues.append(decision.issue)
                review.verdict='revise'
                from .contracts import Check
                affected={s.question_id for s in decision.scopes}
                review.questions=[Check(id=q.id,verdict='issue',evidence=decision.issue.detail)
                    if q.id in affected and q.verdict!='unknown' else q for q in review.questions]
        check_review(review,source)
        self.audit('deferred_review_settled',{'origins':[b['sha256'] for b in payload['deferred_bundles']],
            'current_hash':payload['current_hash'],'decisions':result.model_dump()})
        return review

    def adjudicate(self, source, candidate, observation, failures, original_review=None):
        """Independent evidence-based decision; never edit mathematics or the diagram."""
        self.audit("graph_node", {"node":"adjudicate", "failed_assertions":len(failures)})
        compact_decisions=bool(self.active_review_delta and original_review is not None)
        def validate(raw):
            from .compact_adjudication import unpack as unpack_decisions
            result=unpack_decisions(raw,original_review) if compact_decisions else Adjudication.model_validate(raw)
            exact_ids([d.assertion_id for d in result.decisions],[f["id"] for f in failures],"断言裁决")
            by_id={f["id"]:f for f in failures}
            for d in result.decisions:
                item=by_id[d.assertion_id]
                if item.get("kind") == "review_issue":
                    exact_ids([scope.question_id for scope in d.scopes],[scope.question_id for scope in d.scopes],"异议小问作用域")
                    for scope in d.scopes:
                        if scope.question_id not in {s.question_id for s in candidate.solutions}:
                            raise ValueError(f"{d.assertion_id}须绑定实际小问；可选{[s.question_id for s in candidate.solutions]}")
                        tab=next(i for i,s in enumerate(candidate.solutions) if s.question_id==scope.question_id)
                        objects={o["id"] for o in candidate.spec["tabs"][tab]["board"]["objects"]}
                        if not set(scope.object_ids)<=objects:
                            raise ValueError(f"{d.assertion_id}/{scope.question_id}必须绑定当前小问真实object_ids")
                        try:validate_basis(self.ledger,scope.basis_ids,scope.question_id)
                        except ValueError as exc:raise ValueError(f"{d.assertion_id}/{scope.question_id}: {exc}") from exc
                else:
                    question_id=candidate.solutions[item["tab"]].question_id
                    validate_basis(self.ledger,d.basis_ids,question_id)
                if d.verdict=="real_issue" and d.issue is None: raise ValueError("真实问题必须明确来源/数学/构图的修复目标")
                if d.verdict!="real_issue" and d.issue is not None: raise ValueError("非真实问题不能附带修图要求")
            if original_review is not None:
                from .objection_review import validate_resolution
                validate_resolution(result, original_review, source)
            elif result.reviewed is not None:
                raise ValueError("仅断言裁决不得改写原复核")
            return result
        before=len(self.records)
        result=self.validated("adjudicate", {"source":source.model_dump(),"candidate":candidate.model_dump(),
            "browser_observation":observation,"failed_assertions":failures,"original_review":original_review.model_dump() if original_review else None,"evidence_ledger":self.ledger,
            "compact_decisions":compact_decisions,"reuse_certificate":self.active_review_delta if compact_decisions else None},validate)
        from .inspection_recovery import reconcile
        from .scene_tools import fingerprint
        from .rendering import strict_spec
        inspections=[r for r in self.records[before:] if r["kind"]=="tool" and r.get("name")=="inspect_scene"]
        recovery=reconcile(inspections,fingerprint(strict_spec(candidate.spec)))
        self.audit("adjudication_recovery",recovery)
        if recovery["unresolved"]:
            raise ValueError("断言裁决中的真实图形工具失败且尚未完成对应查询，不能发布")
        n=len(list(self.directory.glob("adjudication-*.json")))+1
        write_json(self.directory/f"adjudication-{n:02d}.json",{"failures":failures,**result.model_dump()})
        self.audit("adjudication",result.model_dump())
        return result

    def execute(self):
        revision = self.directory / "revision.json"
        if revision.is_file():
            from .revisions import validate_revision
            data=json.loads(revision.read_text())
            source,candidate,feedback=validate_revision(data,self.request)
            self.resume_state={"entry":"diagram","source":source,"candidate":candidate,"feedback":feedback,"repairs":0}
            write_json(self.directory/"source.json",source.model_dump())
            self.save_ledger(source,candidate)
            self.audit("explicit_revision", {"original_id":data["original_id"],"feedback":feedback,
                "detail":"显式修订图形，沿用原题与数学解答；重新观察和复核，不能改写数学解答"})
        cache_key = key_for(self.settings, self.request)
        cached = None if self.request["force"] or self.resume_state else self.cache.get(cache_key)
        output = self.directory / "output"
        if cached and self.deferred_reviews.pending:
            raise ValueError('仍有暂存复核义务，不得用缓存绕过，也未自动转为付费调用')
        if cached:
            self.audit("cache", {"status": "hit", "detail": "只重跑本地核验和网页引擎，不重新调用模型"})
            from .native_constructions import require_output
            require_output(cached['source'],self.settings.native_constructions)
            source = Source.model_validate(cached["source"])
            candidate = Candidate.model_validate(cached["candidate"])
            review = Review.model_validate(cached["review"])
            check_source(source, self.request["images"])
            check_candidate(candidate, source)
            if self.settings.parallel_questions:
                if 'question_plan_metadata' not in cached:raise ValueError('并发缓存缺少原依赖计划，不能放宽范围或自动付费重算')
                self.question_plan_metadata=copy.deepcopy(cached['question_plan_metadata'])
                self.validate_parallel_candidate(source,candidate)
            check_review(review, source)
            if review.verdict != "pass": raise ValueError("缓存中没有通过复核的数据")
            from .review_reuse import check_certificate_chain
            self.review_certificates=copy.deepcopy(cached.get('review_certificates',[]))
            check_certificate_chain(source,review,self.review_certificates)
            from .math_review_scope import check_receipts
            self.math_review_receipts=copy.deepcopy(cached.get('math_review_receipts',[]))
            check_receipts(self.math_review_receipts,source)
            if self.settings.parallel_review:
                from .parallel_review import check_rounds
                self.parallel_review_rounds=copy.deepcopy(cached.get('parallel_review_rounds',[]))
                check_rounds(self.parallel_review_rounds,source,candidate,self.request,self.math_review_receipts)
                write_json(self.directory/'parallel-review-rounds.json',self.parallel_review_rounds)
            if self.math_review_receipts:write_json(self.directory/'math-review-receipts.json',self.math_review_receipts)
            if self.review_certificates:write_json(self.directory/'review-certificates.json',self.review_certificates)
            if canonical_page(copy.deepcopy(candidate), source) != candidate: raise ValueError("缓存展示内容与解答不一致")
            from .explanation_coverage import require as require_explanation
            require_explanation(candidate)
            from .native_constructions import require as require_constructions
            require_constructions(source,candidate)
            self.save_ledger(source,candidate)
            # A cache hit may NEVER implicitly fall back to paid calls.
            self.renderer(candidate.spec, output)
        else:
            if not self.settings.api_key: raise RuntimeError("尚未配置 DEEPSEEK_API_KEY")
            self.audit("cache", {"status": "forced" if self.request["force"] else "miss"})
            from .flow import execute_graph
            if self.settings.reference_mode and not self.resume_state:
                from .reference_flow import execute_reference_graph
                state = execute_reference_graph(self)
            else:
                state = execute_graph(self, self.resume_state)
            source, candidate, review = state["source"], state["candidate"], state["review"]
            self.complete_ocr()
            if self.shared_budget is not None:self.shared_budget.check()
            if self.deferred_reviews.pending:raise ValueError('仍有暂存复核义务，不能写缓存或发布')
            from .native_constructions import require as require_constructions
            require_constructions(source,candidate)
            self.validate_parallel_candidate(source,candidate)
            if self.settings.parallel_review:
                from .parallel_review import check_rounds
                check_rounds(self.parallel_review_rounds,source,candidate,self.request,self.math_review_receipts)
            self.cache.put(cache_key, {"source": source.model_dump(), "candidate": candidate.model_dump(), "review": review.model_dump(),
                **({'question_plan_metadata':copy.deepcopy(self.question_plan_metadata)} if self.settings.parallel_questions else {}),
                "parallel_review_rounds":self.parallel_review_rounds,
                "tool_evidence": [r for r in self.records if r["kind"] == "tool"], "deferred_reviews":self.deferred_reviews.dump(), "review_certificates":self.review_certificates, "math_review_receipts":self.math_review_receipts, "model_calls": self.model_calls})
        if self.deferred_reviews.pending:raise ValueError('仍有暂存检查，不得发布网页')
        if self.shared_budget is not None:
            self.shared_budget.check()
            write_json(self.directory/'shared-budget.json',self.shared_budget.snapshot())
        for name, data in [("source", source), ("candidate", candidate), ("review", review)]:
            write_json(self.directory / (name+".json"), data.model_dump())
        report = {"status": "completed", "runtime": "langgraph", "workflow":"reference" if self.settings.reference_mode and not self.resume_state else "classic", "model": self.settings.model, "cache_hit": bool(cached),
            "model_calls_this_job": self.model_calls, "seconds": round(time.monotonic()-self.started, 3),
            "source": source.model_dump(), "review": review.model_dump(), "review_certificates":self.review_certificates, "math_review_receipts":self.math_review_receipts, "deferred_reviews":self.deferred_reviews.dump(), "audit": self.records,
            "cached_tool_evidence": cached.get("tool_evidence", []) if cached else [],
            "render_check": json.loads((output / "export-check.json").read_text()) if (output / "export-check.json").is_file() else None,
            "render_check_scope": "真实 JSXGraph 对象和有限参数采样，不代表整题数学正确或连续区间证明",
            "limitations": ["框架和浏览器观察不保证数学证明正确；模型仍需选对待验关系", "复核来自同一模型的新上下文，不能保证消除同源错误", "坐标工具校验给定实例，不是一般几何证明", "网页自动检查不等于真实数学正确率验收"]}
        write_json(self.directory / "report.json", report)
        self.store.finish(self.job_id, "completed", summary={k: report[k] for k in ["model", "cache_hit", "model_calls_this_job", "seconds"]})

    def resume_review(self):
        """Explicit operator action only: budget-exhausted, fully rendered candidate.

        No HTTP request is replayed. Re-render and independent review are mandatory;
        previous model usage and repairs remain part of the same job's budget.
        """
        if self.settings.parallel_questions or self.settings.parallel_review:
            raise ValueError('并发实验不支持重置原任务预算续跑；须显式新建隔离任务，不重放旧请求')
        prior = json.loads((self.directory / "report.json").read_text())
        row = self.store.get(self.job_id)
        local_observation_collision = (prior.get("error", "").startswith("[Errno 17] File exists:")
            and str(self.directory / "observations" / "scene-") in prior["error"]
            and any(r["kind"] == "explicit_resume" for r in prior.get("audit", [])))
        if row["status"] != "failed" or not (prior.get("error", "").startswith("达到模型调用预算上限") or local_observation_collision):
            raise ValueError("只能显式接续调用预算耗尽的失败任务")
        records = prior["audit"]
        nodes = [r.get("node") for r in records if r["kind"] == "graph_node"]
        if not nodes or nodes[-1] != "review":
            raise ValueError("没有完整渲染后等待复核的候选")
        source = Source.model_validate(json.loads((self.directory / "source.json").read_text()))
        candidate = Candidate.model_validate(json.loads((self.directory / "candidate.json").read_text()))
        check_source(source, self.request["images"]); check_candidate(candidate, source)
        if source.uncertainties: raise NeedsInput("原题仍有未决问题")
        calls = prior["model_calls_this_job"]
        if self.settings.max_calls <= calls: raise ValueError("接续必须显式增加调用上限")
        with self.store.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            if c.execute("SELECT count(*) FROM jobs WHERE status IN ('running','queued')").fetchone()[0]:
                raise ValueError("接续前需等待其他任务完成")
            changed = c.execute("UPDATE jobs SET status='running',stage='resume_review',error=NULL WHERE id=? AND status='failed'", (self.job_id,))
            if changed.rowcount != 1: raise ValueError("任务状态已变化")
        write_json(self.directory / f"report-before-resume-{calls}.json", prior)
        self.records = records
        self.started -= prior["seconds"]
        self.provider.calls = calls
        self.observation_count = len(list(self.directory.glob("observation-*.json")))
        self.resume_state = {"source":source, "candidate":candidate,
            "repairs":sum(r["kind"] == "repair" for r in records), "feedback":[]}
        self.audit("explicit_resume", {"stage":"review", "previous_calls":calls,
            "max_calls":self.settings.max_calls, "detail":"显式补充预算；复用已完成候选，重新渲染和独立复核，不重发旧请求"})
        self.run()

    def run(self):
        try:
            self.execute()
        except Exception as exc:
            # Keep error finite and strip configured credentials even from third-party diagnostics.
            detail = str(exc)[:4000]
            for secret in [self.settings.api_key, self.settings.access_token]:
                if secret: detail = detail.replace(secret, "[REDACTED]")
            status = "needs_input" if isinstance(exc, NeedsInput) else "failed"
            write_json(self.directory / "report.json", {"status": status, "error": detail, "audit": self.records,
                "model_calls_this_job": self.model_calls, "seconds": round(time.monotonic()-self.started, 3)})
            self.store.finish(self.job_id, status, error=detail)
        finally:
            if self.pending_ocr is not None:self.pending_ocr.close()
            if self.shared_budget is not None:
                write_json(self.directory/'shared-budget.json',self.shared_budget.snapshot())
            self.provider.close()
