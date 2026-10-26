# 完整流程诊断：先找问题，再测 180 秒目标

现有 V2 分支新增独立的 **`--diagnostic`** 测试开关。普通测试、生产启动命令、Docker/Compose/API/Worker 都不改。没有调用付费模型验证此改动。

## 直接开始

```bash
git pull --ff-only origin feat/evidence-v2-optimized

# 预览：零模型请求
bash scripts/test_compare.sh 01 --diagnostic

# 运行：第01题，A1 classic 与 B0 V2 各一个全新付费任务
bash scripts/test_compare.sh 01 --diagnostic --run

# 只诊断 V2，减少重复测试费用
bash scripts/test_compare.sh 01 --profiles B0 --diagnostic --run
```

仍自动读取项目 `.env`，配对 `tests/test-items/01.png` 与 `01答案.png`，结果写入新的 `benchmark-runs/时间-随机后缀/`。不需要手工删除 `.env` 中的时间或 Token 设置，也不要把它们改成 0。后续编号和范围仍支持 `02`、`01-03`、`all`；建议先完成单题诊断，避免一次消耗整套题费用。

## 诊断模式到底改变什么

| 限制 | 诊断模式 |
|---|---|
| 整题业务截止时间 | **没有**；不是调到 3600 秒，也不是用 Infinity 欺骗 Worker |
| 阶段时间分配/复核时间预留 | 不启用，不因剩余不足55秒提前强迫模型收尾 |
| Chat `max_tokens` | 不发送，使用模型服务端默认值 |
| Responses `max_output_tokens` | 不发送，使用模型服务端默认值 |
| 原模型、思考开关、思考强度 | 不改变 |
| 模型调用次数、修复次数、每阶段工具循环次数 | 沿用现有保护，不允许无限循环 |
| HTTP 无响应保护 | 1200秒；不是整题总时限 |
| OCR、浏览器、数学子进程限时/资源限制 | 保留，防止本地工具卡死 |
| 自动重试/自动恢复 | 没有；中断后不会再自动花费一次 |
| 独立复核和发布门禁 | 保留；真实错误不跳过，截断JSON仍拒绝发布 |

**不传输出额度 ≠ 无限输出。** 服务端仍有默认输出额度和上下文上限，仍可能返回 `length`。官方说明见 https://api-docs.deepseek.com/api/create-chat-completion/ 。诊断目标是取消本项目额外施加的输出和业务时间截断，不绕过服务商限制。

`--diagnostic` 是显式付费授权之外的第二个开关：仍须 `--run` 才调用模型。模式只通过 benchmark 子进程的程序化配置启用；给普通生产服务设置类似 `AGENT_DIAGNOSTIC_MODE=true` 的环境变量不会关闭生产截止时间。

本轮运行可能明显超过3分钟并增加费用。`Ctrl+C` 会停止当前本地任务进程组和后续测试；已发给模型服务的请求仍可能计费，不能保证撤回云端计算。

## 看哪些结果

终端和 `summary.html` 会明确标记诊断模式；`summary.json` 的 `completed_within_180s` 只是完整返回是否在180秒内，**不是人工确认数学正确，也不是限时验收通过**。失败、未运行、在途不明仍单列。

每个任务保留有效配置、原始审计、错误、模型调用/Token量及原有阶段记录。有效配置中的 `job_timeout` 与输出额度为 `null`，`output_limit_policy=provider_default`，原本 `.env` 的未生效限制另存 `inactive_configured_limits`；没有写入 `Infinity` 或捏造服务端实际输出上限。

先运行到成功或真实失败，定位具体阶段；修复问题后显式另开任务。原题、模型和推理强度相同的两组才适合比较。诊断结果不要与普通限时模式混算。

取消 `--diagnostic` 即恢复原来的有限预算测试：

```bash
bash scripts/test_compare.sh 01 --run
```

这不会修改 `.env`。普通测试的额度仍由 `.env` 决定，旧12,000额度仍可能截断；诊断模式也不等于已经实现2～3分钟优化目标。
