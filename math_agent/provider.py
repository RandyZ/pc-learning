"""Bounded DeepSeek Chat Completions client; no implicit paid retries."""
from __future__ import annotations
import json
import time
import httpx
from .execution_limits import validation, task_timeout, call_limit


class ModelError(RuntimeError):
    pass


class CompletedSchemaError(ModelError):
    """Complete object retained as untrusted input for a caller's strict validator.

    This never retries transport and never means that the candidate is valid.
    Incomplete responses, invalid JSON and non-object output cannot use this path.
    """
    def __init__(self,value,path):
        self.value=value
        self.path=list(path)
        super().__init__('Responses返回完整JSON对象，但不符合输出Schema；尚未通过本地契约与复核')


def _api_error(response, settings):
    """Keep bounded API diagnostics, never image payloads or configured credentials."""
    import re
    try:
        value=response.json()
        value=value.get('error',value) if isinstance(value,dict) else {}
        message=value.get('message','') if isinstance(value,dict) else ''
        code=value.get('code') if isinstance(value,dict) else None
        message=re.sub(r'data:image/[^;\s]+;base64,[A-Za-z0-9+/=]+','[IMAGE REDACTED]',str(message))
        for secret in (settings.api_key,settings.access_token):
            if secret:message=message.replace(secret,'[REDACTED]')
        return {'code':str(code)[:80] if code is not None else None,'message':message[:1500]}
    except (ValueError,TypeError):return {'message':'non-JSON error body not retained'}


def _strip_code_fence(text):
    """Remove one wrapping ```/```json fence; content inside is returned verbatim."""
    t = text.strip()
    if not t.startswith("```"):
        return text
    nl = t.find("\n")
    if nl == -1:
        return text
    t = t[nl + 1:]
    if t.rstrip().endswith("```"):
        t = t.rstrip()[:-3]
    return t.strip()


def _anthropic_messages(history):
    """Convert OpenAI-style chat history to Anthropic messages protocol.

    reasoning_content is never sent back; tool results become tool_result blocks.
    """
    system, messages = [], []
    for m in history:
        role, content = m.get("role"), m.get("content")
        if role == "system":
            if isinstance(content, str) and content:
                system.append(content)
            continue
        if role == "tool":
            block = {"type": "tool_result", "tool_use_id": m.get("tool_call_id", ""),
                     "content": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)}
            if messages and messages[-1]["role"] == "user" and messages[-1].get("_tool_results"):
                messages[-1]["content"].append(block)
            else:
                messages.append({"role": "user", "content": [block], "_tool_results": True})
            continue
        if role == "assistant":
            blocks = []
            if isinstance(content, str) and content:
                blocks.append({"type": "text", "text": content})
            for c in m.get("tool_calls") or []:
                f = c.get("function", {})
                try:
                    args = json.loads(f.get("arguments") or "{}")
                except (ValueError, TypeError):
                    args = {"_raw": f.get("arguments")}
                blocks.append({"type": "tool_use", "id": c.get("id", ""), "name": f.get("name", ""), "input": args})
            messages.append({"role": "assistant", "content": blocks or [{"type": "text", "text": " "}]})
            continue
        if isinstance(content, str):
            messages.append({"role": "user", "content": content})
            continue
        blocks = []
        for b in content or []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                blocks.append({"type": "text", "text": b.get("text", "")})
            elif b.get("type") == "image_url":
                url = (b.get("image_url") or {}).get("url", "")
                media, data = (url.split(",", 1) + [""])[:2] if url.startswith("data:") else ("", "")
                mt = media[5:media.find(";")] if media.startswith("data:") and ";" in media else "image/png"
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": mt, "data": data}})
        messages.append({"role": "user", "content": blocks})
    return "\n\n".join(system), [{k: v for k, v in m.items() if k != "_tool_results"} for m in messages]


class DeepSeek:
    def __init__(self, settings, audit, transport=None):
        self.settings, self.audit = settings, audit
        self.calls = 0
        self.started = time.monotonic()
        self.review_started = False
        self.shared_budget = None
        self._budget_lease = None
        from .image_transport import PixelTiles
        self.pixel_cache=PixelTiles()
        self.client = httpx.Client(timeout=httpx.Timeout(self.settings.http_timeout, connect=15), transport=transport)

    def close(self):
        self.client.close()

    def remaining_seconds(self):
        if validation(self.settings):
            limit = task_timeout(self.settings)
            local = None if limit is None else limit - (time.monotonic() - self.started)
            shared = self.shared_budget.remaining_seconds() if self.shared_budget is not None else None
            values = [v for v in (local, shared) if v is not None]
            return min(values) if values else None
        reserve = self.settings.review_reserve_seconds
        shared = self.shared_budget.remaining_seconds() if self.shared_budget is not None else None
        if not reserve: return shared
        # Before the first review, include OCR/source/tools in the same allowance.
        end = self.settings.job_timeout - (reserve + 5 if not self.review_started else 0)
        result = end - (time.monotonic() - self.started)
        return min(result, shared) if shared is not None else result

    def request_timeout(self, remaining):
        if remaining is not None:
            return httpx.Timeout(max(.01, remaining), connect=min(15, max(.01, remaining)))
        if validation(self.settings):
            # Generation may be long; connection/write/pool failures still terminate.
            return httpx.Timeout(None, connect=15, write=self.settings.http_timeout, pool=15)
        return self.settings.http_timeout

    def remaining_model_calls(self):
        if self.shared_budget is not None:
            return self.shared_budget.remaining_calls
        limit = call_limit(self.settings)
        return None if limit is None else limit - self.calls

    def before_transport(self,stage):
        # Payload/pixel preparation may outlast an admitted request's deadline.
        # Check at the HTTP boundary, not only before local preparation begins.
        from .parallel_budget import ParallelBudgetError
        try:
            if self._budget_lease is not None:self._budget_lease.check()
            remaining=self.remaining_seconds()
            if remaining is not None and remaining<=0:raise ModelError('发送前整题或阶段已截止，未发出模型请求')
            return remaining
        except (ParallelBudgetError,ModelError):
            if self._budget_lease is not None:self._budget_lease.record_not_sent('deadline_or_cancellation_before_http')
            self.audit('model',{'stage':stage,'call':self.calls,'not_sent':True,
                'error':'deadline_or_cancellation_before_http','detail':'本地预处理后检查拒绝发送，没有调用HTTP，也没有编造API usage'})
            raise

    def generate(self, stage, messages, tools=None, dispatch=None, output_schema=None):
        if stage in {"review", "math_review", "graph_review", "review_plan", "adjudicate"}:
            self.review_started = True
        from .agent_loop import run_loop
        fast_draft = (stage == "solve" and self.settings.solve_draft and self.settings.thinking
                      and (validation(self.settings) or self.settings.max_calls-self.calls >= 2))
        initial = True
        def request(history, definitions):
            nonlocal initial
            fast = fast_draft and initial
            initial = False
            # DeepSeek drops prior reasoning when tools is absent. Keep the schema
            # in final output-only requests, while forbidding calls via tool_choice.
            final = definitions is None and bool(tools)
            return self.request(stage, history, tools if final else definitions,
                                thinking_override=False if fast else None,
                                tool_choice="none" if final else None, output_schema=output_schema)
        return run_loop(request, messages, tools, dispatch, self.audit, stage, ModelError,
                        remaining_calls=self.remaining_model_calls,
                        verify_initial_output=fast_draft,
                        remaining_seconds=self.remaining_seconds if validation(self.settings) or self.settings.review_reserve_seconds or self.shared_budget is not None else None,
                        max_turns=None if validation(self.settings) else 4)

    def effort_for_stage(self,stage):
        if self.settings.review_effort and stage in {'review','math_review','graph_review','diagram_review','adjudicate','deferred_review'}:
            return self.settings.review_effort
        return self.settings.effort

    def request(self, stage, history, tools=None, *, thinking_override=None, tool_choice=None, json_mode=True, output_schema=None):
        if self.shared_budget is None:
            return self._request(stage, history, tools, thinking_override=thinking_override,
                tool_choice=tool_choice, json_mode=json_mode, output_schema=output_schema)
        from .parallel_budget import ParallelBudgetError
        try:
            with self.shared_budget.request(stage) as lease:
                self._budget_lease = lease
                try:
                    result = self._request(stage, history, tools, thinking_override=thinking_override,
                        tool_choice=tool_choice, json_mode=json_mode, output_schema=output_schema)
                    lease.check()
                    return result
                finally:
                    self._budget_lease = None
        except ParallelBudgetError as exc:
            raise ModelError(str(exc)) from None

    def _request(self, stage, history, tools=None, *, thinking_override=None, tool_choice=None, json_mode=True, output_schema=None):
        limit = call_limit(self.settings)
        if limit is not None and self.calls >= limit:
            raise ModelError("达到模型调用预算上限；请查看已记录的阶段和异议")
        remaining = self.remaining_seconds()
        if remaining is not None and remaining <= 0:
            raise ModelError("阶段时间预算已用完；未追加请求，未发布未经复核结果")
        self.calls = self._budget_lease.call_id if self._budget_lease is not None else self.calls + 1
        thinking = self.settings.thinking and stage not in {"source", "review_plan", "reference_math", "draw", "draw_patch", "graph_review"} and (stage != "diagram" or self.settings.diagram_thinking)
        if thinking_override is not None:
            if type(thinking_override) is not bool:
                raise ValueError('thinking_override必须为bool或None')
            thinking = thinking_override
        tokens = self.settings.source_max_tokens if stage == "source" else self.settings.max_tokens
        if stage in {"solve", "math_repair", "diagram"} and self.settings.solve_max_tokens:
            tokens = self.settings.solve_max_tokens
        elif stage in {"review", "review_plan", "math_review", "graph_review", "adjudicate"} and self.settings.review_max_tokens:
            tokens = self.settings.review_max_tokens
        if validation(self.settings):
            tokens = self.settings.validation_max_tokens
            if tokens < 512:
                raise ModelError("验证模式尚未解析模型输出上限；未发送请求，请使用评测入口或 --max-output-tokens")
        if self.settings.api_style=='deepseek':
            from .image_transport import prepare_messages
            prepared_at=time.perf_counter()
            try:history,tiles=prepare_messages(history,self.pixel_cache)
            except (ValueError,OSError) as exc:raise ModelError('原图传输预检失败；未发出请求：'+str(exc)[:300]) from None
            if tiles:self.audit('image_transport',{'stage':stage,'call':self.calls,'tiles':tiles,
                'preparation_seconds':round(time.perf_counter()-prepared_at,6),'pixel_preparation':self.pixel_cache.snapshot(),
                'detail':'超限原图由总览和覆盖全部像素的裁片传输；任务原图和角色不变'})
        if output_schema is not None:
            enabled=(stage in {'review','deferred_review','math_repair'} and self.settings.review_responses
                     or stage in {'solve','diagram'} and self.settings.solve_responses)
            if not enabled:
                raise ModelError("Responses schema仅允许显式开启的阶段")
            return self._responses_request(stage, history, tools, tool_choice, tokens, remaining, thinking, output_schema)
        if self.settings.api_style == "glm":
            return self._anthropic_request(stage, history, tools, tool_choice, json_mode, tokens, remaining, thinking)
        body = {"model": self.settings.model, "messages": history,
                "max_tokens": tokens,
                "thinking": {"type": "enabled" if thinking else "disabled"}}
        if type(json_mode) is not bool:raise ValueError("json_mode必须为布尔值")
        if json_mode:body["response_format"] = {"type": "json_object"}
        if thinking:
            body["reasoning_effort"] = self.effort_for_stage(stage)
        if tools:
            body["tools"] = tools
        if tool_choice is not None:
            if tool_choice != "none" or not tools:raise ValueError("仅允许在保留工具声明时设置tool_choice=none")
            body["tool_choice"] = tool_choice
        from .image_transport import check_body
        try:check_body(body)
        except ValueError as exc:raise ModelError(str(exc)) from None
        remaining=self.before_transport(stage)
        start = time.monotonic()
        self.audit("model_budget", {"stage":stage, "call":self.calls, "max_tokens":tokens,
            "remaining_seconds":round(remaining,3) if remaining is not None else None,
            "review_started":self.review_started})
        try:
            response = self.client.post(self.settings.base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": "Bearer " + self.settings.api_key}, json=body,
                timeout=self.request_timeout(remaining))
        except httpx.TimeoutException:
            self.audit("model", {"stage":stage,"call":self.calls,"error":"stage_timeout"})
            raise ModelError("模型请求超出阶段时间预算；未自动重发，未发布未经复核结果，已发请求可能仍计费") from None
        except httpx.HTTPError as exc:
            self.audit("model", {"stage": stage, "call": self.calls, "error": type(exc).__name__})
            raise ModelError("模型网络请求失败；为避免重复计费，没有自动重发") from None
        elapsed = round(time.monotonic() - start, 3)
        if response.status_code != 200:
            self.audit("model", {"stage": stage, "call": self.calls, "status": response.status_code, "seconds": elapsed,"api_error":_api_error(response,self.settings)})
            hint = {401: "Key 无效", 402: "账户余额不足", 403: "账号无访问权限", 429: "模型限流"}.get(response.status_code, "请核对模型名、图片能力和请求配置")
            raise ModelError(f"模型接口 HTTP {response.status_code}：{hint}；未自动重试")
        try:
            result = response.json()
            if self._budget_lease is not None:
                self._budget_lease.record_usage(result.get('usage'))
            choice = result["choices"][0]
            message, finish = choice["message"], choice["finish_reason"]
            if not isinstance(message, dict): raise TypeError("invalid message")
            self.audit("model", {"stage": stage, "call": self.calls, "model": result.get("model"),
                "tools_present":bool(tools), "tool_choice":tool_choice, "json_mode":json_mode,
                "prior_reasoning_chars":sum(len(m.get("reasoning_content") or "") for m in history),
                "thinking": thinking, "reasoning_effort": self.effort_for_stage(stage) if thinking else None,
                "usage": result.get("usage", {}), "seconds": elapsed, "finish_reason": finish})
            if remaining is not None and self.remaining_seconds() <= 0:
                raise ModelError("模型返回时已超过阶段时间预算；已记录用量，未把迟到输出当作完成")
            if finish == "length":
                raise ModelError(f"模型输出被截断（本次 max_tokens={tokens}）；API输出/上下文上限仍有效，未自动重试或发布不完整答案")
            if finish == "stop":
                content = message.get("content")
                from .json_framing import close_completed_containers,audit_closure
                framed=close_completed_containers(content)
                if framed is not None and stage=='review' and self.settings.compact_tools:
                    from .compact_review import repair_encoding
                    if repair_encoding(content) is not None:framed=None
                if framed is not None:
                    audit_closure(self.audit,stage,self.calls,content,framed,'chat')
                    content=framed;message={**message,'content':framed}
                try:
                    parsed = json.loads(content) if isinstance(content,str) else None
                    if not isinstance(parsed,dict):
                        raise ValueError("最终输出不是JSON对象")
                except (ValueError,TypeError) as exc:
                    # Keep the returned answer, never reasoning_content, so a failed
                    # parse is diagnosable without replaying a paid request.
                    raw = content if isinstance(content,str) else repr(content)
                    for secret in (self.settings.api_key,self.settings.access_token):
                        if secret:raw=raw.replace(secret,"[REDACTED]")
                    detail = str(exc)
                    for secret in (self.settings.api_key,self.settings.access_token):
                        if secret:detail=detail.replace(secret,"[REDACTED]")
                    self.audit("model_output_error", {"stage":stage,"call":self.calls,
                        "error":detail[:500],"position":getattr(exc,"pos",None),
                        "content_chars":len(raw),"content_truncated":len(raw)>200000,
                        "content":raw[:200000]})
                    if stage=="review" and self.settings.compact_tools and isinstance(content,str):
                        from .compact_review import repair_encoding
                        repaired=repair_encoding(content)
                        if repaired is not None:
                            import hashlib
                            encoded=json.dumps(repaired,ensure_ascii=False,allow_nan=False)
                            message={**message,'content':encoded}
                            self.audit('json_encoding_repair',{'stage':stage,'call':self.calls,
                                'method':'keyed-review-member-punctuation; exact scalar sequence preserved',
                                'before_sha256':hashlib.sha256(content.encode()).hexdigest(),
                                'after_sha256':hashlib.sha256(encoded.encode()).hexdigest(),
                                'detail':'仅修复JSON成员包装标点；所有键、值、核验文字和异议顺序逐项原样保留，随后仍严格校验'})
            return message, finish
        except (KeyError, IndexError, TypeError, json.JSONDecodeError):
            raise ModelError("模型响应结构或 JSON 格式无效") from None

    def _responses_request(self, stage, history, tools, tool_choice, tokens, remaining, thinking, schema):
        from dataclasses import replace
        from .responses_transport import body, message as decode_message
        from .compact_output import unique_object
        from jsonschema import validate, ValidationError
        from .image_transport import check_body
        try:
            payload = body(replace(self.settings, max_tokens=tokens,effort=self.effort_for_stage(stage)), history, schema, tools, tool_choice, thinking,
                           strict=(stage in {'solve','diagram'} and self.settings.solve_responses
                                   or stage=='review' and self.settings.review_responses))
            check_body(payload)
        except ValueError as exc:
            if self._budget_lease is not None:self._budget_lease.record_not_sent('responses_schema_or_payload_preflight')
            self.audit('model',{'stage':stage,'call':self.calls,'not_sent':True,'transport':'responses',
                'error':'responses_schema_or_payload_preflight','detail':str(exc)[:1000]})
            raise ModelError('Responses发送前预检失败，未调用HTTP：'+str(exc)[:1000]) from None
        remaining=self.before_transport(stage)
        start = time.monotonic()
        self.audit("model_budget", {"stage":stage,"call":self.calls,"max_tokens":tokens,
            "remaining_seconds":remaining,"review_started":self.review_started,"transport":"responses",
            "schema_strict":payload['text']['format']['strict']})
        try:
            response = self.client.post(self.settings.base_url.rstrip("/")+"/responses",
                headers={"Authorization":"Bearer "+self.settings.api_key},json=payload,
                timeout=self.request_timeout(remaining))
        except httpx.HTTPError as exc:
            self.audit("model",{"stage":stage,"call":self.calls,"error":type(exc).__name__,"transport":"responses"})
            raise ModelError("Responses网络请求失败；没有自动重发，已发请求可能计费") from None
        elapsed=round(time.monotonic()-start,3)
        if response.status_code != 200:
            self.audit("model",{"stage":stage,"call":self.calls,"status":response.status_code,"seconds":elapsed,"transport":"responses","api_error":_api_error(response,self.settings)})
            raise ModelError(f"Responses接口HTTP {response.status_code}；未回退或重试")
        result={}
        try:
            result=response.json()
            if not isinstance(result,dict):raise TypeError("Responses响应必须是对象")
            if self._budget_lease is not None:
                self._budget_lease.record_usage(result.get('usage'))
            self.audit("model",{"stage":stage,"call":self.calls,"model":result.get("model"),
                "transport":"responses","thinking":thinking,"reasoning_effort":self.effort_for_stage(stage) if thinking else None,
                "tools_present":bool(tools),"tool_choice":tool_choice,"usage":result.get("usage",{}),
                "seconds":elapsed,"api_status":result.get("status")})
            if remaining is not None and self.remaining_seconds() <= 0:
                raise ModelError("Responses返回时已超出阶段时间；未发布")
            msg,finish=decode_message(result)
            if finish=='stop':
                try:
                    value=json.loads(msg['content'],object_pairs_hook=unique_object)
                except json.JSONDecodeError:
                    # Same lossless member-wrapper recovery as Chat. Only a
                    # completed compact review is eligible; no partial response,
                    # changed scalar, duplicate key or paid replay is accepted.
                    from .json_framing import close_completed_containers,audit_closure
                    from .compact_review import repair_encoding
                    repaired=(repair_encoding(msg['content']) if stage=='review' and self.settings.compact_tools else None)
                    framed=None
                    if repaired is None:
                        framed=close_completed_containers(msg['content'])
                        if framed is not None:
                            audit_closure(self.audit,stage,self.calls,msg['content'],framed,'responses')
                            repaired=json.loads(framed,object_pairs_hook=unique_object)
                    if repaired is None:raise
                    import hashlib
                    encoded=framed if framed is not None else json.dumps(repaired,ensure_ascii=False,allow_nan=False)
                    if framed is None:self.audit('json_encoding_repair',{'stage':stage,'call':self.calls,'transport':'responses',
                        'method':'keyed-review-member-punctuation; exact scalar sequence preserved',
                        'before_sha256':hashlib.sha256(msg['content'].encode()).hexdigest(),
                        'after_sha256':hashlib.sha256(encoded.encode()).hexdigest(),
                        'detail':'完整输出只修复成员括号；所有键、值、异议逐字保留，随后严格Schema及原编号校验'})
                    value=repaired
                    msg={**msg,'content':encoded}
                validate(value,schema)
            return msg,finish
        except (ValueError,TypeError,KeyError,ValidationError) as exc:
            import re
            def redact(value,limit):
                text=str(value)
                text=re.sub(r'data:image/[^;\s]+;base64,[A-Za-z0-9+/=]+','[IMAGE REDACTED]',text)
                for secret in (self.settings.api_key,self.settings.access_token):
                    if secret:text=text.replace(secret,'[REDACTED]')
                return text[:limit]
            # Only the final answer is retained, never reasoning output or request history.
            data=result if isinstance(result,dict) else {}
            items=data.get('output',[])
            items=items if isinstance(items,list) else []
            final=''.join(str(p.get('text','')) for item in items if isinstance(item,dict) and item.get('type')=='message'
                for p in (item.get('content') or []) if isinstance(p,dict) and p.get('type')=='output_text')
            details=data.get('incomplete_details') or {}
            self.audit('model_output_error',{'stage':stage,'call':self.calls,'transport':'responses',
                'api_status':data.get('status'),'incomplete_reason':redact(details.get('reason','') if isinstance(details,dict) else '',300),
                'error':redact(exc,1000),'content_chars':len(final),'content_truncated':len(final)>200000,
                'content':redact(final,200000)})
            if isinstance(exc,ValidationError) and data.get('status')=='completed' and isinstance(value,dict):
                raise CompletedSchemaError(value,exc.absolute_path) from None
            raise ModelError("Responses输出未完成或不符合阶段Schema；未自动回退或重发") from None

    def _anthropic_request(self, stage, history, tools, tool_choice, json_mode, tokens, remaining, thinking):
        """GLM coding-plan endpoint: Anthropic messages protocol.

        The endpoint accepts thinking {"type": "enabled"/"disabled"} (unlike the
        paas/v4 API, code 1210); the stage-level switch follows the same policy
        as the DeepSeek path. The response is mapped back to the OpenAI message
        shape so the agent loop, audit and JSON gates stay protocol-agnostic.
        """
        system, messages = _anthropic_messages(history)
        body = {"model": self.settings.model, "max_tokens": tokens, "messages": messages,
                "thinking": {"type": "enabled" if thinking else "disabled"}}
        if system:
            body["system"] = system
        if tools and tool_choice != "none":
            body["tools"] = [{"name": t["function"]["name"],
                              "description": t["function"].get("description", ""),
                              "input_schema": t["function"].get("parameters") or {"type": "object", "properties": {}}}
                             for t in tools]
            body["tool_choice"] = {"type": "auto"}
        remaining=self.before_transport(stage)
        start = time.monotonic()
        self.audit("model_budget", {"stage": stage, "call": self.calls, "max_tokens": tokens,
            "remaining_seconds": round(remaining, 3) if remaining is not None else None,
            "review_started": self.review_started})
        try:
            response = self.client.post(self.settings.base_url.rstrip("/") + "/v1/messages",
                headers={"x-api-key": self.settings.api_key, "anthropic-version": "2023-06-01"},
                json=body,
                timeout=self.request_timeout(remaining))
        except httpx.TimeoutException:
            self.audit("model", {"stage": stage, "call": self.calls, "error": "stage_timeout"})
            raise ModelError("模型请求超出阶段时间预算；未自动重发，未发布未经复核结果，已发请求可能仍计费") from None
        except httpx.HTTPError as exc:
            self.audit("model", {"stage": stage, "call": self.calls, "error": type(exc).__name__})
            raise ModelError("模型网络请求失败；为避免重复计费，没有自动重发") from None
        if remaining is not None and self.remaining_seconds() <= 0:
            raise ModelError("模型返回时已超过阶段时间预算；未把迟到输出当作完成")
        elapsed = round(time.monotonic() - start, 3)
        if response.status_code != 200:
            self.audit("model", {"stage": stage, "call": self.calls, "status": response.status_code, "seconds": elapsed})
            hint = {401: "Key 无效", 402: "账户余额不足", 403: "账号无访问权限", 429: "模型限流或额度不足"}.get(response.status_code, "请核对模型名、图片能力和请求配置")
            raise ModelError(f"模型接口 HTTP {response.status_code}：{hint}；未自动重试")
        try:
            result = response.json()
            blocks = result["content"]
            text = "\n".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text").strip()
            calls = []
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    calls.append({"id": b.get("id", ""), "type": "function",
                                  "function": {"name": b.get("name", ""),
                                               "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)}})
            stop = result.get("stop_reason")
            finish = "length" if stop == "max_tokens" else "tool_calls" if calls or stop == "tool_use" else "stop"
            usage = result.get("usage") or {}
            self.audit("model", {"stage": stage, "call": self.calls, "model": result.get("model"),
                "tools_present": bool(tools), "tool_choice": tool_choice, "json_mode": json_mode,
                "prior_reasoning_chars": 0, "thinking": thinking, "reasoning_effort": None,
                "usage": {"prompt_tokens": usage.get("input_tokens"),
                          "completion_tokens": usage.get("output_tokens"),
                          "total_tokens": (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)},
                "seconds": elapsed, "finish_reason": finish, "stop_reason": stop,
                "thinking_blocks": sum(1 for b in blocks if isinstance(b, dict) and b.get("type") == "thinking")})
            if finish == "length":
                raise ModelError(f"模型输出被截断（本次 max_tokens={tokens}）；API输出/上下文上限仍有效，未自动重试或发布不完整答案")
            if finish == "stop" and text:
                stripped = _strip_code_fence(text)
                if stripped != text:
                    self.audit("glm_fence_strip", {"stage": stage, "call": self.calls,
                        "detail": "GLM 输出包在代码围栏里；仅剥离围栏，内容逐字保留"})
                    text = stripped
            message = {"role": "assistant", "content": text if text else None}
            if calls:
                message["tool_calls"] = calls
            return message, finish
        except (KeyError, IndexError, TypeError, json.JSONDecodeError):
            raise ModelError("模型响应结构或 JSON 格式无效") from None
