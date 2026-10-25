# Evidence V2：独立优化流程与对照测试

本分支基于 `d23b43549e5a897a6e348da130b546c6f10b80db`。**默认仍是 classic；没有修改生产部署，也没有运行付费 DeepSeek 验收。** 离线测试通过不代表数学正确率或 120～180 秒产品目标达标。

## 1. 本版实现

```text
source → propose_solution → prepare_execution（公共计算）
                              ├─ check_math ───────────────┐
                              └─ compile_and_observe ──────┤
                                                          ↓
                                                    join_evidence
                                                          ↓
                                                  independent_review
                                                   ├─ publish
                                                   └─ repair → prepare/source
```

- 解题一次提交证明、白名单计算计划及高层构图计划，不增加固定规划/排版 Agent。
- 计算依赖拓扑排序；图形所需标量先执行，然后数学检查与图形编译/真实浏览器可以并行。
- 数学操作：simplify、evaluate、solve、equivalent、substitute_check、endpoints。固定 AST 白名单，单子进程通道，每批最多12项、墙钟最多12秒、内存512MiB；整个任务仍受原 Worker 总截止约束。返回 `execution_status` 与 `verdict`，不能把运行成功当数学证明；未证明、域不明、超时是 unknown。
- 构图宏：rotate_polygon、translate_polygon、reflect_group、function_with_moving_point。机械展开原生 JSXGraph 对象、对应边和标签，保存 source map。不能猜坐标、方向、函数域。未覆盖宏使用同一输出中的合法原生 objects；不自动切换另一付费流程。
- 任务内精确计算结果复用：键绑定来源、操作、变量假设、定义域、工具代码版本、依赖结果和相关证明哈希。负证据仍是负证据；错误/超时不缓存成通过。复用生成 `reused_from`，不伪装新执行。
- ChangeSet 使用期望修订哈希及小问/字段授权，保留未改内容；数学不能擅自改图，图形不能改证明；不能删除已有计算义务或把检查降为普通计算。影响范围包含新旧跨问依赖后继。
- 保留原独立复核、原图查看、浏览器观察、异常检查恢复、独立裁决和数学优先暂存。新增 `operation_reviews` 覆盖每项计算；修订后 `revision_reviews` 显式解除旧异议。unknown 只能用明确的独立证明补证，不把插件不支持当通过。
- 发布检查校验当前 source/draft/candidate、数学证据、复核、浏览器产物版本及全部未决事项。成品缓存与 classic 隔离；损坏缓存失败，不偷偷重问模型。

### 诚实边界

本版为 **可执行的 B0/B1/B2 第一版**，不是通用数学证明器。数学计划的来源语义仍须独立复核；SymPy solve 输出候选根，不声称穷尽所有实数/参数情形。旧 source 与复杂 review/adjudication 实现暂以桥接方式复用。

`pipeline.py` 现在是薄兼容入口；原 Runner 字节不变保存在 `legacy_pipeline.py`。新增 V2 业务实际放在独立模块，而非把旧 Runner 换目录就宣称重构完成。classic 主图/工具循环保持原样。

**第一版对所有 Patch 仍执行完整独立复核和完整页面重渲染。** 已实现精确数学证据复用及变更范围计算，但没有宣称完成“仅变化图形截图复用”或“局部复核”的全部后续优化。这样先保留发布安全边界，再单独测试进一步缩减范围。

未接入 Newclid；没有改变 OCR 策略，没有启用小问模型并行或分组模型复核，没有降低思考强度或 token 额度。不能保证实际调用数固定为3；工具补证、有限格式纠正、裁决、返修都计入同一预算。

## 2. 工程职责

| 包 | 职责 |
|---|---|
| application/job_service.py | 依赖装配、冻结有效配置、成品缓存验证 |
| workflows/evidence_v2 | 明确主图、State/Context、节点、同版本并行汇合 |
| domain/v2.py | Draft、Proof、Operation、ScenePlan、ChangeSet、复核契约 |
| policies/v2.py | DAG、来源/步骤引用、稳定ID、影响范围、修订权限 |
| services | 解题、批量计算、复核、独立发布服务 |
| compilers/scene.py | 宏展开、标量引用、source map、原生对象校验 |
| models | 复用 DeepSeek 传输与调用预算；V2 提示词与扩展复核协议 |
| infrastructure/v2_math.py | 有界数学子进程，不运行模型生成代码 |
| observability | 脱敏配置、正确计时、可选 metadata-only LangSmith |
| benchmarks | 隔离任务、对照组、人工质量导入、可选 Math-Verify |

图状态只有可序列化题设、计划、证据和修订信息；客户端、锁、Key、图片 Base64 保留在 Runtime Context/任务文件中。不使用 LangGraph checkpoint 自动重放付费节点。

## 3. 运行配置（不修改生产 .env）

V2 必须显式选择：

```dotenv
AGENT_WORKFLOW=evidence_v2
AGENT_V2_PROFILE=serial
AGENT_COMPACT_TOOLS=true
```

`AGENT_V2_PROFILE` 可选 `serial`、`parallel`、`parallel_reuse`。V2 第一版禁止与以下旧实验开关叠加：reference、review_plan、solve_draft、parallel_questions、parallel_review、overlap_ocr、incremental_review、incremental_math_review。对照脚本会明确设置这些架构开关并记录最终配置。

保持自己的 `DEEPSEEK_MODEL`、`DEEPSEEK_BASE_URL`、`DEEPSEEK_THINKING`、`DEEPSEEK_REASONING_EFFORT`、token/调用/修复/时间预算不变。Chat 和显式启用的 Responses 协议均有离线契约测试，但真实供应商兼容性仍需用户验证。

## 4. 推荐对照命令

在项目根目录，使用原来的虚拟环境。Key 只放本地环境变量，不作为命令参数，也不提交 Git。所有 --out 路径必须是新目录；脚本禁用应用成品缓存，保留并记录 OCR/Provider 前缀缓存实际情况。

```bash
# 只准备输入和配置，绝不请求模型。
python -m benchmarks.run --profile B0 --case-id case01 \
  --question /path/question.png --answer /path/answer.png \
  --out benchmark-runs/case01-B0-prepare --prepare-only

# 以下每条 --allow-paid 都显式执行一个有预算的新任务；会产生真实模型费用。
python -m benchmarks.run --profile A1 --case-id case01 \
  --question /path/question.png --answer /path/answer.png \
  --out benchmark-runs/case01-A1 --allow-paid
python -m benchmarks.run --profile B0 --case-id case01 \
  --question /path/question.png --answer /path/answer.png \
  --out benchmark-runs/case01-B0 --allow-paid
python -m benchmarks.run --profile B1 --case-id case01 \
  --question /path/question.png --answer /path/answer.png \
  --out benchmark-runs/case01-B1 --allow-paid
python -m benchmarks.run --profile B2 --case-id case01 \
  --question /path/question.png --answer /path/answer.png \
  --out benchmark-runs/case01-B2 --allow-paid
```

A1=行为兼容的 classic；B0=V2串行；B1=B0+本地两分支并行；B2=B1+精确计算复用。A0 是原始提交，需要单独 checkout 固定基线运行原入口，不能把本分支 A1 冒充 A0。

A1 采用你已有的 classic 策略。V2 明确切换上述不兼容架构开关；模型和预算不覆盖。比较程序会警告输入字节、模型/协议/思考/预算不一致。B0/B1/B2 的推理配置和输出契约相同，只比较执行策略。A1→B0 同时改变了计划与构图协议，不能把收益拆成某一个组件的单项成果。

脚本通过**原 Worker supervise 和原 run-job 命令**运行，保持进程组、取消、硬截止等行为，不直接绕过 Worker。生产服务和数据库不受影响。每次新建独立 runtime，且 force=true，不重复使用之前的 Source/Solve 冒充新题。

每组至少记录：manifest.json（输入哈希、实际代码及配置）、result.json（全量状态与时间）、runtime/jobs/<id>/report.json、perf.json、v2-draft.json、v2-source-map.json、v2-evidence.json、v2-operation-resolution.json、v2-change-*.json。

这些完整本地产物可能含题图、解答和模型内容，**不要直接上传公开仓库或公开 Actions artifact**。本分支 CI 只上传离线测试的 JUnit。

## 5. 质量与成本比较

人工评分文件按实际 job_id 填写：

```json
{"实际job_id": {"math":true,"proof":true,"diagram":true,"interaction":true,"coverage":true}}
```

```bash
python -m benchmarks.compare benchmark-runs/case01-*/result.json \
  --quality /private/quality.json --out /private/comparison.json
```

无人工评分的自动 pass 不计为已确认正确；失败/超时留在分母。主要指标为“180秒内完整正确交付率”，在全部人工评分完成前只给已确认下界。小样本只显示中位数及范围，不给稳定P95承诺。费用默认未知，不把缺失 usage 算成零；后续人工补充一致币种的账单费用再评估每个正确交付总成本。

可选 Math-Verify：在单独评测环境安装 `pip install '.[evaluation]'`，调用 `benchmarks.evaluators.math_verify.compare(gold,prediction)`。输出 matched/mismatch/unparseable/checker_error，受子进程 CPU/内存/时间限制。它只比较答案，不能替代证明、图形、定义域与单位的人工核验；未安装时明确 checker_error。

## 6. LangSmith 与 Studio

生产/命令行默认不上传。V2 只有显式设置 `LANGSMITH_TRACING=true` 且提供 `LANGSMITH_API_KEY` 才创建 metadata-only Trace；可用 `LANGSMITH_PROJECT` 指定项目。上报节点名、数字耗时与 token 用量，不上报提示词、原图、工具参数、完整输出或异常文本。Trace 故障不触发模型重试。没有真实 LangSmith 凭据测试，隐私边界由测试替身检查。

Studio 仅开发使用，不改变生产部署：安装可选 `.[studio]`，建立不入库的 `.env.debug`，**Studio 模式保持 LANGSMITH_TRACING=false**。本地 Agent Server 的状态可供 Studio 查看，不要把它当公网生产服务。

```bash
python -m benchmarks.run --profile B0 --case-id debug01 --text '线段AB=2，M是中点，求AM' \
  --out .studio-runs/debug01 --prepare-only
langgraph dev --config langgraph.json
```

图名 evidence_v2。只加载拓扑不会创建模型客户端；执行时需在 configurable 设置 `studio_task="debug01"`、`allow_paid_model_calls=true`。每个准备目录只允许执行一次；不支持从已付费 checkpoint 继续或分叉重放，须显式新建任务。Studio 用于节点调试；**正式时延/硬截止验收必须用 benchmarks.run 的原 Worker 路径**。Studio 与生产的传输和复核服务相同，不代表 Studio 进程拥有原 Worker 的进程组硬终止保障。

## 7. 验证和回退

```bash
python -m pytest -q
MATH_AGENT_BROWSER_TEST=1 python -m pytest -q  # 需要Chromium与redis-server
```

CI 显式将 DeepSeek Key 置空，仅模型响应被模拟，浏览器、Redis、SQLite、API、Worker、数学子进程为真实执行。部署文件、API/CLI/Worker 字节差异由 CI 单独检查。

生产回退：新任务选择 `AGENT_WORKFLOW=classic`；已有任务不在中途换图。缓存按工作流和代码版本隔离。没有进行生产部署、没有改任何 Secret、没有使用聊天中曾提供的 Key。
