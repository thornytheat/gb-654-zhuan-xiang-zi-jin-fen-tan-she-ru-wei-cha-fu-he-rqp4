# 深度登记 专项资金分摊与舍入尾差复核

        - 规模：中型
        - 核心规则：先迭代封顶再按有效权重分配，最后用最大余额法落到分；输入顺序变化不得改变结果。
        - 使用者与工作流：导入随仓样例，逐模块复核，最后导出单一证据清单。

        ## 功能模块

        | 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
        |---|---|---|---|---|
        | M1 预算采信 | 运行样例并复核证据 | `/api/budgets/import` / `/budgets` | `app/modules/m1_budgets/core.py` `app/modules/m1_budgets/service.py` | `tests/test_m1_budgets.py` |
| M2 资格校验 | 运行样例并复核证据 | `/api/eligibility/evaluate` / `/eligibility` | `app/modules/m2_eligibility/core.py` `app/modules/m2_eligibility/service.py` | `tests/test_m2_eligibility.py` |
| M3 权重测算 | 运行样例并复核证据 | `/api/weights/calculate` / `/weights` | `app/modules/m3_weights/core.py` `app/modules/m3_weights/service.py` | `tests/test_m3_weights.py` |
| M4 金额分摊 | 运行样例并复核证据 | `/api/allocations/calculate` / `/allocations` | `app/modules/m4_allocations/core.py` `app/modules/m4_allocations/service.py` | `tests/test_m4_allocations.py` |
| M5 尾差裁决 | 运行样例并复核证据 | `/api/remainders/resolve` / `/remainders` | `app/modules/m5_remainders/core.py` `app/modules/m5_remainders/service.py` | `tests/test_m5_remainders.py` |
| M6 批次复核 | 运行样例并复核证据 | `/api/export/allocations.csv` / `/runs` | `app/modules/m6_runs/core.py` `app/modules/m6_runs/service.py` | `tests/test_m6_runs.py` |
| M7 跨批次尾差结算 | 预览、结算并回读结转欠额 | `/api/settlement/preview` `/api/settlement/commit` / `/settlement` | `app/modules/m7_settlement/core.py` `app/settlement_store.py` | `tests/test_m7_settlement_core.py` `tests/test_m7_settlement_store.py` |

        ## 陷阱登记

        | TRAP | 现象 | 朴素实现 | 本项目处理 | 处理位置 | 测试 |
        |---|---|---|---|---|---|
        | TRAP-05 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m1_budgets/core.py` | `tests/test_m1_budgets.py::test_trap_1` |
| TRAP-06 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m2_eligibility/core.py` | `tests/test_m2_eligibility.py::test_trap_1` |
| TRAP-07 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m3_weights/core.py` | `tests/test_m3_weights.py::test_trap_1` |
| TRAP-09 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m4_allocations/core.py` | `tests/test_m4_allocations.py::test_trap_1` |
| TRAP-10 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m5_remainders/core.py` | `tests/test_m5_remainders.py::test_trap_1` |
| TRAP-14 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m6_runs/core.py` | `tests/test_m6_runs.py::test_trap_1` |
| TRAP-19 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m1_budgets/core.py` | `tests/test_m1_budgets.py::test_trap_2` |
| TRAP-22 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m2_eligibility/core.py` | `tests/test_m2_eligibility.py::test_trap_2` |
| TRAP-24 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m3_weights/core.py` | `tests/test_m3_weights.py::test_trap_2` |
| TRAP-26 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m4_allocations/core.py` | `tests/test_m4_allocations.py::test_trap_2` |
| TRAP-31 | 连续小批尾差偏向编码靠前项目 | 每批独立最大余额法，平票恒取小编号 | 尾分竞争序纳入同年度历史欠额，跨批轮转补偿 | `app/modules/m7_settlement/core.py` | `tests/test_m7_settlement_core.py::test_tail_rotates_across_batches_no_permanent_bias` |
| TRAP-32 | 套最新欠额复算旧批次会改旧账 | 结算后用现行台账重算历史批次 | 原裁决、结转前后欠额整批落档，回读只还原存档 | `app/settlement_store.py` | `tests/test_m7_settlement_store.py::test_settled_batch_recompute_restores_original_verdict` |
| TRAP-33 | 并发结算重复结转 | 先算后写无版本控制 | `BEGIN IMMEDIATE` 串行化 + 乐观版本号拒绝过期提交 | `app/settlement_store.py` | `tests/test_m7_settlement_store.py::test_concurrent_commit_rejects_stale_version` |
| TRAP-34 | 中断重试产生半笔或双份结转 | 多表分步写入、重试重复入账 | 裁决与欠额单事务提交；同批次重试幂等回放已存裁决 | `app/settlement_store.py` | `tests/test_m7_settlement_store.py::test_retry_after_interruption_replays_without_double_carry` |
| TRAP-35 | 欠额跨年污染 | 全局单一欠额表 | 台账以专项年度为作用域，新年度从零起算 | `app/modules/m7_settlement/core.py` | `tests/test_m7_settlement_core.py::test_carry_over_only_within_same_fund_year` |
| TRAP-36 | 封顶项目被尾分顶破上限 | 尾分只看零头不看剩余额度 | 达限即退出尾分竞争，欠额暂存、未发出尾分记批次未分配 | `app/modules/m7_settlement/core.py` | `tests/test_m7_settlement_core.py::test_capped_project_parks_and_never_breaches_cap` |

        ## 对账规则

        - 输入记录数 = 有效记录数 + 隔离记录数 + 空记录数。
        - 核心数值或事件数在转换、判定和导出之间保持守恒。
        - 同一输入摘要重跑复用原运行编号，不增加结果行。
        - M7 批次守恒：实配合计 + 未分配 = 批复总额；项目欠额差额 = 精确份额 − 实发金额。
        - M7 欠额按专项年度隔离；版本号每次提交 +1，预览不变更版本与台账。
        - M7 回读以存档裁决为准，后续批次的欠额变化不得改写历史批次金额。

        ## 已知不覆盖

        - 生产系统连接、身份认证和外部密钥不在演示范围内。
