# 重构分支：按题号执行与评测

入口：`python scripts/evaluate.py`。这是 classic/LangGraph 重构版的评测，不是 V2 的 B0/B1/B2。

题库从原图片提交 `c4bdeaa2a525799993475c311c0946fb2241aae3` 的 `tests/test-items/` 原样同步；只复用图片 Git 对象，不合并 V2 业务代码。自动匹配 `01.png` 和 `01答案.png`，也支持 `01答案-1.png`、`01答案-2.png`。无答案图可以运行；图片不会被当作人工确认的标准答案。

## 首次准备

在本分支根目录，用原有 Python 3.12 虚拟环境即可；未安装时：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -c constraints.txt -e '.[test]'
python -m playwright install chromium
# Linux 缺浏览器依赖：python -m playwright install --with-deps chromium
```

把模型 Key 和原本已经跑通的模型配置放在本地 `.env`。脚本会自动读取，不必 `source .env`；支持单行 `KEY=value`、引号、注释、`export KEY=value`，不会执行 Shell，也不展开 `${变量}` / `$(命令)`。已有环境变量优先，冲突时只提示变量名，不打印值。另一个环境文件用 `--env /private/test.env`。

保持 `DEEPSEEK_MODEL`、接口和思考强度与原配置一致。默认 `validation` 模式只解除生产预算，详细策略如下；`--budget-mode production` 才沿用原来的token、调用和修复预算。当前分支不支持 `AGENT_WORKFLOW=evidence_v2`，复制 V2 配置时需移除该项或改成 `classic`。已配置 OCR 时需保持服务可用，不会失败后静默跳过。

## 最常用命令

```bash
# 查看所有题目，不调用模型
python scripts/evaluate.py list

# 先检查题图配对、图片解码及生效配置：零模型请求，不启动浏览器/OCR
python scripts/evaluate.py run 1 --dry-run

# 真实执行第1题：run 本身即显式授权这一题的模型费用
python scripts/evaluate.py run 1

# 批量顺序运行，失败保留后继续下一题，不重试失败题
python scripts/evaluate.py run 1-5
python scripts/evaluate.py run 1,3,8-10

# 全部题，可能产生较多费用；先用单题确认配置和效果
python scripts/evaluate.py run all
```

## 完整验证与生产预算分开

`python scripts/evaluate.py run 1` 默认使用 **validation（完整验证）**：

| 限制 | 完整验证 | production 对照 |
|---|---|---|
| 单题总时间 | 无应用截止；Ctrl+C停止 | `.env` 的 `AGENT_JOB_TIMEOUT` |
| 总模型调用、主流程返修 | 不沿用生产次数上限 | 原配置 |
| 阶段工具往返 | 不因第4轮强制提交 | 原4轮限制 |
| 模型输出 | `GET /models` 返回的当前模型 `max_output_tokens`；覆盖source/solve/review等阶段低额度 | 原全局和阶段配置 |
| HTTP读取等待 | 不用300秒截断正在生成的响应 | 原HTTP配置 |
| 连接、写入、工具沙箱、图片大小、契约和发布检查 | 保留 | 保留 |

`run` 会先读取模型元数据，不发送生成请求来探测额度。代理不返回 `max_output_tokens`、模型名找不到或查询失败时明确停止，**不静默退回12000/64000**。可显式指定供应商支持的上限：

```bash
# 默认：完整验证，沿用原模型和思考强度
python scripts/evaluate.py run 1
# 对照生产限制（严格读取 .env 中原来的预算）
python scripts/evaluate.py run 1 --budget-mode production
# 验证过程中自愿设置单题截止；0表示不设
python scripts/evaluate.py run 1 --timeout 600
# 第三方端点不提供模型上限时，明确指定其支持的值
python scripts/evaluate.py run 1 --max-output-tokens 64000
```

输出上限不是固定消耗，模型正常结束即停止；它也不是无限输出。API上下文/输出硬上限、供应商网络超时仍可能导致失败，不能保证任何题必定完成。HTTP失败、截断不会自动重发；主流程可以继续实际需要的修复，因此完整验证可能明显超过180秒并产生更多费用。先测单题，发现反复无进展时Ctrl+C，不建议直接放任全题库运行。

读取图片最多8次、浏览器观察最多12次等单工具防循环保护，以及单次数学/浏览器/OCR执行的资源和超时保护仍保留。有限格式纠正、未知题意处理和独立复核规则不变，不用放行错误来换完成率。

`--dry-run` 不查询模型元数据；未显式给额度时显示 `auto`。所有真实运行在 `manifest.json`、`report.html` 和任务 `effective-config.json` 中写入 `execution_limits`，其中 `null` 表示无应用预算；原 `max_tokens` 等字段仍记录配置值，实际请求额度以 `execution_limits.model_output_tokens` 和 `model_budget` 审计为准。对照会提示模式与额度不同，不能把验证模式成功直接视为生产180秒验收通过。

自定义题库用 `--fixtures /path/to/items`。六图及上传容量限制保持原接口规则。

每次自动新建 `eval-runs/时间戳-随机编号/`；也可 `--out /private/new-run`，但目录必须不存在。禁止覆盖旧运行或自动续跑已计费请求。

## 执行和隔离

不必启动 API、后台 Worker 服务或 Redis。脚本使用原 Worker `supervise` 和原 `run-job` 子进程，沿用原进程组和取消机制；仅显式设定截止或production模式才使用任务硬截止；SQLite 与任务目录隔离在本次新建目录内。原生产配置、端口和部署文件不变。

脚本强制当前评测进程的 `AGENT_DATA_DIR` 为新目录、`REDIS_URL=''`、请求 `force=true`，并关闭远端追踪以避免干扰测速；不写回 `.env`。此外通过仅当前进程/子进程的 `AGENT_VALIDATION_*` 标记传递所选评测策略；正常 API/Worker 默认不启用验证模式。应用成品缓存不使用，OCR/供应商前缀缓存可能仍命中，实际值见本地记录。

开始付费前预检浏览器/OCR，并校验所选全部图片。控制台显示每题阶段、耗时、调用次数和错误。`Ctrl+C` 停止当前任务、停止启动后续题并保留报告；云端已发送请求仍可能计费。鉴权/余额/权限错误也停止剩余题，其他题目失败继续，不自动重试。不支持原生 Windows，请用 WSL2。不要同时启动两批性能测试争抢机器资源。

## 看结果与人工评测

```text
eval-runs/<本轮>/
├── report.html          # 直接浏览器打开：每题状态、耗时、讲解链接
├── summary.csv          # 表格汇总
├── summary.json         # 自动完成率、已确认正确率及180秒指标
├── manifest.json        # 代码/输入版本、脱敏生效配置
├── runs.json            # 所选每题记录，失败和未启动题均保留
├── quality.csv          # 人工评分模板，默认留空，不能自动当作通过
├── cases/01/result.html # 仅成功任务复制完整讲解；不会发布失败草稿
├── cases/01/result.json # 单题耗时、调用、token、阶段及失败原因
└── runtime/jobs/...     # 原始题图、报告和审计，供本地诊断
```

单题计时为进入隔离任务队列至完整产物就绪；整批图片准备时间不计入单题时延。模型耗时使用 `duration_seconds`，不是累计时间戳之和。未返回 usage 的请求保留未知；费用不按猜测价格计算。

自动 `completed` 仅表示流程通过，不证明解题正确。在 `quality.csv` 对数学结果、证明、图形、交互、覆盖五列填写 `true`/`false`，保留 `case_id`，不确定时留空，然后：

```bash
python scripts/evaluate.py report eval-runs/<本轮>
# 比较两次由本脚本生成的运行；不发模型请求，也不重跑
python scripts/evaluate.py compare eval-runs/<运行A> eval-runs/<运行B>
```

正确率是人工确认的下界；失败/超时在已启动请求分母内，未启动题单列。无人工评分时不会把自动通过率冒充准确率。对照会提示输入或有效模型配置差异；失败提前结束不算提速。此 compare 不直接读取 V2 旧 benchmarks 格式。

`eval-runs/` 已加入 `.gitignore`。自定义输出目录需自行保护；报告和完整原始记录可能含题图与模型正文，不上传到公开仓库或公开 Actions Artifact。没有使用聊天中提供的 Key，不会在 CI 中跑真实模型。

模型元数据依据：https://api-docs.deepseek.com/api/list-models/ （max_output_tokens 为当前接口接受的上限；运行时读取，不硬编码某个模型的固定值）。
