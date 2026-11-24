# batch48-py-safety-b-10

## 题面

审计留存治理上线后，法务与运维来投诉：在一起诉讼保全期间，本该冻结的登录审计仍然被夜里跑的销毁任务清掉了；另一批记录按写入批次计算保留期，晚到的事件刚入库就被判到期；按类别销毁时删了与他租户共享的物理段，隔壁租户记录一起消失；销毁只留一行计数，法务核对删了哪几条时无人能答；索引删了正文还在，查询接口又把这批记录翻了出来；保留策略从 365 天收紧到 90 天后，历史事件没有按事件时刻重算，仍然沿用旧的批次边界；跨租户的冻结范围被串用，一个租户的冻结令挡住了另一个租户的正常销毁；时钟回拨后任务提前执行，未到期记录被清掉。范围是 gsb/batch48-py-safety-b-10 的 audit_retention_guard.py，类 RetentionGuard，负责按租户与类别配置保留策略、登记法律冻结、执行到期销毁并出具可核对凭据。

公开接口：RetentionGuard(*, max_records=500, max_holds=32, clock=None)；set_policy(tenant, category, days) -> dict；policy_for(tenant, category) -> dict；add_record(tenant, category, at, record_id=None) -> dict；hold(tenant, scope, until, reason) -> dict；release(hold_id, now=None) -> dict；hold_status(hold_id) -> dict；purge(now) -> dict；query(tenant, start, end) -> list；describe_receipt(receipt_id) -> dict；snapshot() -> dict；restore(state) -> None；stats() -> dict。拒绝统一抛 RetentionError(code, detail)。

返回结构与入参：set_policy 返回 tenant、category、days 与 version，version 从 1 起每次变更加一；add_record 返回 id、tenant、category、at 与 expires；hold 返回 hold_id、tenant、scope、until、reason 与 status，编号从 hold-1 起，scope 取 tenant 或 category:<类别>；release 返回 hold_id、status 与 released_at；purge 凭据返回 receipt_id、at、count、removed、blocked、blocked_count 与 tenants，编号从 purge-1 起，每次请求都出具一张；hold_status 返回冻结全部字段；query 返回按事件时刻升序、同刻按编号排列的记录；snapshot 返回 version、policies、records、holds、receipts、counters 与 last_purge_at；stats 返回 records、holds、holds_active、purged 与 receipts；未注入时钟时当前时刻按 0 计。

故障与边界：一、冻结期内仍被销毁必须拦住，阻断的记录要进入凭据的阻断清单；二、保留期按事件时刻加策略天数计算，不能按写入批次或入库顺序；三、同一物理段被多个租户共享时，只有全部租户都到期且无冻结才允许销毁，不能按单租户判定；四、销毁必须留可核对的凭据，凭据可按编号回查，列出被删记录与涉及租户；五、删了索引还要删正文，销毁后查询不得再把记录翻出来；六、策略收紧后必须按事件时刻重新计算到期日，不能沿用旧批次边界；七、冻结范围不得跨租户串用，类别冻结只在本租户存在该类别策略时才成立；八、时钟回拨时销毁要拒绝，已推进的销毁时刻不得倒回；九、相同租户、范围、截止时刻与原因的冻结重复提交要幂等并返回同一张凭据，截止时刻必须晚于当前时刻；十、已释放的冻结不再阻断销毁，重复释放与未知编号都要报错；十一、查询窗口要按租期边界校验，倒置窗口拒绝；十二、记录条数与活动冻结数都是硬顶，超限拒绝而不是覆盖旧记录。

硬约束：purge 与 query 交错时查询不得看到半删记录，六路并发查询与销毁后，被删编号与存活记录必须互斥且不重不漏；快照保存策略版本、冻结范围、销毁凭据与此前销毁时刻，重启后未到期事件不丢、已销毁事件不复现；记录与冻结数量都是硬顶。

验收：python3 -m pytest -q tests 的 15 条必须全过，不得修改测试、跳过或按固定输入写死；共享段与时钟回拨场景自行验证；拒绝码用 RETENTION_POLICY_INVALID、RETENTION_POLICY_MISSING、RETENTION_DUPLICATE_RECORD、RETENTION_TIMESTAMP_INVALID、RETENTION_RECORD_INVALID、RETENTION_RECORD_LIMIT、RETENTION_UNKNOWN_TENANT、RETENTION_WINDOW_INVALID、RETENTION_RECEIPT_UNKNOWN、RETENTION_HOLD_INVALID、RETENTION_HOLD_LIMIT、RETENTION_HOLD_CLOSED、RETENTION_HOLD_UNKNOWN、RETENTION_CLOCK_ROLLBACK、RETENTION_SNAPSHOT_INVALID；只做本地实现，不涉及上游提交。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 系统安全
- type: Bugfix
- difficulty: 困难
- module: audit_retention_guard
- typeName: RetentionGuard
