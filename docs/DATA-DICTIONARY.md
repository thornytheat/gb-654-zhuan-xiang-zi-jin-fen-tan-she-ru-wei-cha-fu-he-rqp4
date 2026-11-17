# 数据字典

| 字段 | 类型 | 可空 | 定义 |
|---|---|---:|---|
| `run_id` | string | 否 | 输入摘要派生的稳定运行编号 |
| `source_line` | integer | 否 | 原文件物理行号 |
| `reason_code` | string | 是 | 隔离或冲突的稳定原因码 |
| `raw_value` | string | 是 | 未归一的原始证据 |
| `normalized_value` | string | 是 | 按项目规则得到的值 |
| `rule_version` | string | 否 | 计算规则版本 |
| `fund_year` | string | 否 | M7 专项年度（4 位），欠额结转的唯一作用域 |
| `batch_id` | string | 否 | M7 批次编号，同年度内唯一且幂等 |
| `expected_version` | integer | 否 | M7 结算乐观版本号，与台账当前版本不一致即拒绝 |
| `deficit_before` | string | 否 | M7 结算前项目欠额（1e-8 元定点，正为少发、负为超发） |
| `deficit_delta` | string | 否 | M7 本批精确份额与实发金额之差 |
| `deficit_after` | string | 否 | M7 结算后结转欠额，下批同年度竞争序输入 |
| `unallocated` | string | 否 | M7 封顶等原因未能发出的批次金额（分整） |
| `parked` | boolean | 否 | M7 项目在尾分竞争中触及当批上限、欠额暂存标记 |

字符串编号逐字保留。金额与计量值用 `Decimal`。时间值带偏移并以 UTC 参与比较。区间统一采用左闭右开。
