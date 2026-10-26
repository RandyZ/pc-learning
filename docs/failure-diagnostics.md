# 对照测试失败诊断：模型输出截断

新版终端和 `summary.html` 会显示失败阶段、`finish_reason`、**该次请求**的 Token 上限、总输出/思考/非思考输出数量。仍保留原始任务报告链接；不解析或发布截断 JSON，不自动重试，不自动扩大额度。

`max_tokens` 不是只限制最终答案：开启思考时，思考也占输出额度。观察到 `completion_tokens=12000`、`reasoning_tokens=12000`、`finish_reason=length` 时，说明该请求额度全用在思考；不能当作已生成完整答案。若记录不足，诊断不会把上下文长度限制误判成输出上限。

预检现在打印各组的实际解题额度、思考强度和 compact 开关。`high/max` 且解题额度不超过 12000 时提示风险；这是来自实测的告警阈值，不是 API 的最低要求。**不会改动你的 `.env` 或生产配置。**

可先对已有记录免费重新汇总，无须 Key 或题图：

```bash
bash scripts/test_compare.sh --report benchmark-runs/某一轮目录
```

确认要另开付费诊断时，可显式给两组相同的 32000 额度，并统一启用 compact（仓库 `compact-high` 预设也采用 32000；它不是保证成功的值）：

```bash
DEEPSEEK_MAX_TOKENS=32000 \
DEEPSEEK_SOLVE_MAX_TOKENS=0 \
DEEPSEEK_REVIEW_MAX_TOKENS=0 \
AGENT_COMPACT_TOOLS=true \
bash scripts/test_compare.sh 01 --run
```

阶段额度为0表示沿用全局额度，不是禁止生成。Shell 环境变量优先于 `.env`，因此上述改动仅作用于这一次脚本，不改模型、high 思考或任务时限。此命令会运行两个新的付费任务，不是接着补完旧任务。更大输出上限可能增加时延和费用；**它只解决过早截断风险，不证明 180 秒内能正确完成。**

原 `.env.example` 中 compact 未开启，而 V2 要求开启：若 A1/B0 的 `compact_tools` 不一致，汇总会明确提示。这仍然是原配置与V2的实验，但不能冒充“compact classic 对 V2”的同条件对照。不能单用两组失败前的耗时判断谁更快。
