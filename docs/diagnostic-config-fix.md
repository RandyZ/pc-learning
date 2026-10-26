# 诊断模式预检修复

## 原因

`create_settings()` 原来仍让 `Settings` 从环境读取并校验已经停用的输出/整题额度。
因此 `DEEPSEEK_MAX_TOKENS=65536`（普通模式代码上限是64000）、`0`、空字符串等，
会在任何模型请求之前阻止诊断任务。旧错误把修复次数和输出额度合并，无法仅凭堆栈区分实际是哪一项错误。

## 本次修改

- 诊断工厂在继承的整数解析/范围校验之前隔离五项停用配置：全局/读题/解题/复核输出额度及整题时限。
- 内部整数占位值仅用于兼容已有适配器，不是实际时限或输出额度；原有 diagnostic 传输仍不发送 cap，监督器仍没有整题截止时间。
- `effective-config.json` 中实际停用项仍为 `null`，数字原值记入 `inactive_configured_limits`；空值和非数字只记录忽略标记，不泄露任意环境文本。
- 不修改 `.env`、不改变模型和思考强度，也不扩大调用/修复/工具预算。
- 仍生效的保护逐项报错：如 `AGENT_MAX_REPAIRS` 必须为0～4、`AGENT_MAX_MODEL_CALLS` 必须为1～30；不会悄悄将超限值改成默认。
- 普通模式默认值、额度范围以及生产 API/Worker/部署文件保持不变。

## 重测

```bash
git pull --ff-only origin feat/evidence-v2-optimized
bash scripts/test_compare.sh 01 --profiles B0 --diagnostic --run
```

`--run` 会运行一个新的付费任务。本次修复没有发起真实模型请求。

不要将 `AGENT_MAX_REPAIRS` 或 `AGENT_MAX_MODEL_CALLS` 配成无限；若它们确实超范围，
新的错误会明确显示变量名和数值。停用的 Token/整题额度不需要为了诊断而手工改回。

## 回归

`tests/test_diagnostic_config_limits.py` 覆盖停用字段的越界/零/空/非数字、保留有效保护、
普通模式不变、配置快照脱敏、`dataclasses.replace` 以及独立 Python 进程读取相同环境。
`tests/v2/test_diagnostic_entry.py` 扩展真实 classic/V2 诊断子进程，使用本机 HTTP 替身验证
越界/空额度不阻塞启动、请求仍省略 Token 上限，并在题意不确定时正确停止且不自动重试。

完整结果以本提交的 CI 为准；配置与离线测试通过不代表真实题数学质量或速度已经达标。
