# Classic LangGraph 结构重构（独立验收分支）

本分支只改变经典流程的职责组织、可观测性和开发入口。不包含 Evidence V2 的计算计划、构图宏、额外并行或精确复用。生产默认的模型、提示词、思考强度、额度、工具白名单和修复规则不变；编号评测入口的完整验证模式另见文末。真实 DeepSeek 速度、数学质量以及真实 LangSmith/Studio 连接尚未验收。

## 为什么独立

`feat/evidence-v2-optimized@2741458d5b02c779276126c18b340065ea46d5eb` 保持原样供用户对照测试。不要将本分支直接 merge/cherry-pick 到 V2：两者的 pipeline.py 和 services 命名有交叉。V2 质量与速度确认后，在新的迁移分支适配这些层次与端口，保留 V2 的计划、编译、证据和发布语义，再运行固定候选及真实题对照。V2 的 A1 是兼容旧实现，不等于本分支。

## 实际拆分

- `pipeline.py`：兼容的 Runner 方法签名与委托，不保存旧 Runner 副本。
- `application/context.py`：任务级连接、预算、工具资源与迁移期状态；不进入图 State。
- `application/job_service.py`：原有缓存验证、任务生命周期、发布和显式恢复。
- `models/gateway.py`：阶段请求组装和协议选择；复用原 provider.py。
- `services/source.py`、`solving.py`、`reviewing.py`、`adjudication.py`：各阶段业务规则。
- `services/validation.py`、`tool_execution.py`、`evidence.py`、`observation.py`：结构纠正、工具权限、证据与浏览器动作。
- `domain/ports.py`、`errors.py`、`presentation.py`：阶段接口、错误及确定性内容投影。
- `workflows/classic/`：State、Runtime Context、独立节点和主图。
- `workflows/tool_loop/`：模型/工具循环的 State、Context、节点和子图。
- `observability/`：有效配置、本地性能统计、可选 metadata-only LangSmith。

原始经典规则不是整体移动到 legacy_pipeline.py。所有阶段函数都接受显式 job runtime，阶段不持有绑定旧 Runner 的 service 实例；这样现有分支浅拷贝和实验子类重载仍有效。source/solve/review 的跨阶段数据保存在图 State，历史复杂复核的迁移期资源保留于 JobContext。

`reference_flow.py` 等既有实验路线不改控制逻辑；原 provider/tools/prompts/contracts 和 JSXGraph 的物理位置保留，避免破坏包资源、子进程 cwd 和旧导入。后续可以逐步迁移，但本版不把这些尚未迁移的旧辅助模块称为已全部重构。

## 行为约束

正常顺序仍为 source → solve → observe → review。来源问题回 source；数学问题回 math_repair；仅图形问题回 diagram；生产模式保持原修复次数和每阶段最多四轮模型请求；显式完整验证模式解除这些预算。模型工具历史的 reasoning_content、工具配对、最后一轮 tool_choice=none 和真实未知项均保留。

没有新增自动 RetryPolicy、付费节点 checkpoint 重放或隐藏模型请求；重启仍按原 Worker 标记 interrupted。缓存键新增迁移代码指纹，防止服务代码修改后沿用不匹配证据；因此与旧版成品缓存隔离，切换后的首次新请求可能不命中旧缓存。后续同版本命中规则不变。

Dockerfile、compose.yaml、deploy/、原 API/CLI、生产预算默认值和启动命令不变。CI 检查部署、提示词、工具与浏览器资产无差异；Worker/Provider 的验证模式适配使用回归测试验证生产行为。

## 测试与统计

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -c constraints.txt -e '.[test]'
python -m playwright install chromium
# 不需要真实 Key，普通测试按环境跳过浏览器/Redis集成项。
DEEPSEEK_API_KEY='' LANGSMITH_TRACING=false python -m pytest -q
# 完整项需要本机 Chromium 与 redis-server；测试使用临时 Redis。
MATH_AGENT_BROWSER_TEST=1 DEEPSEEK_API_KEY='' LANGSMITH_TRACING=false python -m pytest -q
```

等价性测试锁定原版 `d23b43549e5a897a6e348da130b546c6f10b80db`。浅克隆先 `git fetch origin main`。测试逐项检查迁移函数规范化 AST；固定场景还逐字比较原版/重构版提示词、工具 Schema、调用顺序、错误状态与结果。它们不证明远程随机模型响应逐字相同。

原有数据目录和服务命令仍可用于手工测试；与生产隔离数据目录、端口，并先后运行同一题，避免资源争用。没有额外模型配置开关需要切换：本分支的 classic 就是结构重构版。

每个任务新增 `effective-config.json` 和 `perf.json`，不保存凭据。`scripts/summarize_timing.py` 现在读取单请求 `duration_seconds`，不再累加时间戳；支持 Chat/Responses 用量，区分累计工作时长与已知区间并集。缺失用量/耗时保持未知。完整本地报告可能含原题、解答和工具内容，不上传公开仓库。

## 可选 LangSmith

默认无上传。仅在本地环境中同时配置 `LANGSMITH_TRACING=true`、`LANGSMITH_API_KEY` 才创建客户端；可选 `LANGSMITH_PROJECT`。自动图状态追踪仍被禁用，改为显式上报任务/节点/阶段/工具层级，以及模型请求耗时和数字 token 用量。输入、输出、原图、工具参数、推理原文、异常文本不上传。远端 Trace 故障不会触发模型重试，不吞掉业务异常。

本版验证使用捕获客户端检查敏感信息不会进入上报 payload，尚未真实连接 LangSmith。基准测速请关闭远端追踪，以免同步上报开销混入架构比较。

## 可选本地 Studio

```bash
python -m pip install '.[studio]'
# .env.debug 只在本机保存，内容为模型配置，LANGSMITH_TRACING=false。
python -m debug.prepare_classic --text '线段AB=2，M为中点，求AM'
langgraph dev --config langgraph.json
```

图名 `classic`，拓扑加载不创建模型客户端。执行前在 configurable 中指定准备命令返回的 `debug_job_id`，并显式设置 `allow_paid_model_calls=true`。每个任务只允许一次尝试；凭原子锁拒绝重启重放和 checkpoint fork。原题回读的正常修复仍可执行。

这是节点诊断入口，不是生产发布入口。结束后记录为 interrupted（诊断完成，不发布），可以检查节点状态和本地候选，但不会把未经过原任务发布路径的结果标为 completed。异常、超时/取消需要按诊断状态处理；中断后新建任务，不续跑已计费节点。Studio 进程没有原 Worker 的进程组硬终止保障，正式时延和发布验收必须走原 CLI/Worker。真实 Studio Server 尚未启动验证。

## 当前边界

离线测试和真实浏览器/Redis集成测试均使用模型测试替身，不使用聊天中给过的 Key。不能由测试通过推出真实 DeepSeek 成功率或 120–180 秒目标。此分支只为结构验收和后续 V2 结构迁移提供可靠基线，不自动合并或部署。

## 评测预算更新

编号评测入口默认完整验证，使用 `python scripts/evaluate.py run 1 --budget-mode production` 才保留原生产预算。为此 config、Provider、Worker、图循环增加显式验证模式分支；默认关闭时原行为由回归测试和固定基线差分验证，而不再要求这些预算适配文件逐字不变。Dockerfile、Compose、deploy、API/CLI命令、模型/思考默认值、提示词与发布规则未改。详见 `docs/evaluate.md`。
