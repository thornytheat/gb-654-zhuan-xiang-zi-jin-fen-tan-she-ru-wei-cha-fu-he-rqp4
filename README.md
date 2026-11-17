# 专项资金分摊与舍入尾差复核

        面向一线复核人员的单服务数据工作台。样例数据随仓库提供，结论保留行号、原因码和原值。

        ## 业务模块

        - M1 预算采信：`/api/budgets/import`
- M2 资格校验：`/api/eligibility/evaluate`
- M3 权重测算：`/api/weights/calculate`
- M4 金额分摊：`/api/allocations/calculate`
- M5 尾差裁决：`/api/remainders/resolve`
- M6 批次复核：`/api/export/allocations.csv`
- M7 跨批次尾差结算：`/api/settlement/preview`、`/api/settlement/commit`

        ## 核心规则

        先迭代封顶再按有效权重分配，最后用最大余额法落到分；输入顺序变化不得改变结果。

        M7 在最大余额法之上引入同专项年度欠额台账：尾分按“本批零头 + 历史欠额”竞争，
        补偿历次舍入欠额；欠额仅在同一年度内结转，封顶项目欠额暂存且不突破当批上限。
        预览不占欠额，结算携带乐观版本号，裁决与欠额单事务原子生效，重试按已存裁决恢复。
        仅记账与回读，不改资格评分，不接支付。

        ## 启动

        ```bash
        docker compose up --build
        ```

        浏览器访问 `http://localhost:18213`。运行测试：

        ```bash
        docker compose exec -T app pytest -q
        ```

        ## 样例流程

        依次运行六个模块；最后在批次页复核输入、有效、隔离与输出合计。同一输入摘要重复执行时复用原 `run_id`。

        跨批次尾差走 M7：`POST /api/settlement/preview` 试算（回显 `expected_version`，不占欠额），
        再 `POST /api/settlement/commit` 携带该版本号结算；`GET /api/settlement/batch/{年度}/{批次}`
        回读原批次金额与结转前后欠额，`GET /api/settlement/ledger/{年度}` 查看当前欠额与版本。

        ## 已知不覆盖

        不处理加密文件与需要外部密钥的对象；不连接真实生产存储。M7 仅做台账内尾差结算与回读，
        不修改资格评分、不发起任何支付。
