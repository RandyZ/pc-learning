# 01～05 候选金标

这些文件是 **AI 转写并独立复算后的候选金标，待用户人工确认**，不是人工已验收金标。顶层 `verification_status` 因而统一为 `ai_checked_pending_human`。图片路径从仓库根目录解析，SHA-256 对应当前实际图片；答案图只作核对材料，不替代题图，也不直接充当正确性证明。

- `subquestions` 保留原题编号（含①②③），记录最终答案、证明检查点及定义域、分类、端点义务。
- `diagram_requirements` / `interaction_requirements` 只用于相应图形要求；纯函数、代数题不虚构交互。
- `reference_issues` 记录参考答案内部冲突、缺漏或排版异常；没有异常时保留空数组。
- 无法唯一确定的项目应把该问 `final_answer.status` 标为 `needs_review` 并在 `review_notes` 解释。`needs_review` **不计入确认正确**，也不能标记为 verified 或人工确认。

与 `benchmarks.compare --quality` 的五项人工评分衔接：`math` 核对最终答案，`proof` 核对证明检查点和各类义务，`diagram`、`interaction` 分别核对图形与交互要求，`coverage` 核对原小问是否完整覆盖。只有完成人工评分后，才能按比较流程确认质量；本目录的 schema 测试通过不代表数学内容已获人工确认。

Math-Verify 只能辅助比较最终答案，不能证明推导、范围端点、图形、交互或小问覆盖正确。

离线校验：

```bash
.venv/bin/python -m pytest -q tests/v2/test_gold_candidates.py
```
