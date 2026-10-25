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

保持 `DEEPSEEK_MODEL`、思考强度、token、调用和修复预算与原配置一致。脚本不偷偷切模型或覆盖这些策略。当前分支不支持 `AGENT_WORKFLOW=evidence_v2`，复制 V2 配置时需移除该项或改成 `classic`。已配置 OCR 时需保持服务可用，不会失败后静默跳过。

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

自定义题库用 `--fixtures /path/to/items`。默认每题截止时间沿用 `AGENT_JOB_TIMEOUT`；只有显式传 `--timeout 600` 才改为600秒（仅诊断，不意味着达成180秒目标）。六图及上传容量限制保持原接口规则。

每次自动新建 `eval-runs/时间戳-随机编号/`；也可 `--out /private/new-run`，但目录必须不存在。禁止覆盖旧运行或自动续跑已计费请求。

## 执行和隔离

不必启动 API、后台 Worker 服务或 Redis。脚本使用原 Worker `supervise` 和原 `run-job` 子进程，沿用原进程组、取消和硬截止；SQLite 与任务目录隔离在本次新建目录内。原生产配置、端口和部署文件不变。

脚本仅强制当前评测进程的 `AGENT_DATA_DIR` 为新目录、`REDIS_URL=''`、请求 `force=true`，并关闭远端追踪以避免干扰测速；不写回 `.env`。应用成品缓存不使用，OCR/供应商前缀缓存可能仍命中，实际值见本地记录。

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
