# batch48-py-safety-a-17

## 题面

车载盒子在地铁口从 Wi-Fi 切到蜂窝，迁移完成后播放立刻卡住：执行过程记录显示旧路径上迟到的包把序号水位顶回了迁移前，重新编号的包被当成重复丢掉；另一次更糟，迁移票据在宽限期内被第二次使用，服务端照着同一张票据又开了一个纪元，两条路径同时生效，下行花屏。运维还发现，冷却期内拒绝迁移之后，旧路径上漏收的那批包全部被丢弃，客户端只能整段重传；双路径并存时两个不同载荷的同序号包被误判成重复，反向的序号检查又把新包挡在了旧路径之外。

范围是 gsb/batch48-py-safety-a-17 下的 conn_migration_guard.py，类 ConnMigrationGuard，负责会话与路径集合、迁移票据、纪元推进、旧路径宽限期、包接收裁决、RTT 估计重算与快照恢复。

公开接口：`ConnMigrationGuard(*, max_paths_per_session=3, grace_ms=500, cooldown_ms=1000, max_migrations=4, reorder_window=64, clock=None)`；`create_session(session, *, addr, now=0) -> dict`；`issue_ticket(session, *, ttl_ms=2000, now=0) -> dict`；`probe_path(session, addr, *, ticket, now=0) -> dict`；`complete_migration(session, *, now=0) -> dict`；`rollback(session, *, now=0) -> dict`；`on_packet(path, epoch, seq, *, payload=None, now=0) -> dict`；`record_sample(path, rtt_ms, *, now=0) -> dict`；`reset_estimates(session, *, now=0) -> dict`；`path_state(session) -> dict`；`list_paths(session) -> list`；`stats() -> dict`；`snapshot() -> dict`；`restore(state) -> None`。拒绝统一抛 MigrationError(code, detail)。

故障与边界：一、会话号非空且不得重复，重复报 MIGRATION_SESSION_EXISTS，空号或空地址报 MIGRATION_INVALID；票据按 tk-0001 递增，ttl_ms 必须为正，未知会话报 MIGRATION_UNKNOWN_SESSION；二、probe_path 依次校验票据归属、是否存在与是否过期，分别报 MIGRATION_TICKET_MISMATCH、MIGRATION_UNKNOWN_TICKET 与 MIGRATION_TICKET_EXPIRED，被拒时不得新增路径或推进纪元；三、候选路径数已达 max_paths_per_session 时再次探测报 MIGRATION_PATH_LIMIT，未达上限但已有候选路径时报 MIGRATION_PENDING_EXISTS，两种拒绝都保持旧路径不变；四、迁移次数达到 max_migrations 报 MIGRATION_LIMIT_REACHED，冷却期内的探测报 MIGRATION_COOLDOWN，两种拒绝都不得移动活动路径或改动纪元；五、complete_migration 把候选升为活动路径、旧路径转入 retiring 并按 grace_ms 记下截止时间，没有候选时报 MIGRATION_NO_PENDING；六、retiring 路径只在宽限期内接受包，超出宽限期按 stale_epoch 丢弃，冷却期内被拒之后旧路径漏收的包仍要按旧纪元序号被接受；七、on_packet 对未知路径报 MIGRATION_UNKNOWN_PATH，纪元不符按 epoch_mismatch 丢弃；八、同一路径同一纪元下，序号相同的包只有载荷摘要也相同才判为重复，载荷不同按两个独立包各自接受，双路径并存时同一序号在不同路径上各自计数；九、序号低于高水位超过 reorder_window 的包按 too_old 丢弃，窗口内的乱序包正常接受并更新高水位；十、record_sample 对未知路径报 MIGRATION_UNKNOWN_PATH，只接受正整数 RTT，reset_estimates 用新路径样本重估并保留上一次结果供回退，没有样本报 MIGRATION_NO_SAMPLES；十一、rollback 回到上一路径并把候选标为 abandoned，没有可回退的迁移报 MIGRATION_NO_ROLLBACK，票据在宽限期内被第二次使用必须拒绝并计入违规，不得重新开纪元；十二、快照覆盖会话、路径、票据、统计与序号水位，恢复后继续按纪元与序号去重，坏载荷报 MIGRATION_SNAPSHOT_INVALID 且原状态不变。

硬约束：create_session、probe_path、complete_migration、rollback、on_packet 与 restore 全程持锁，六路并发向各自路径灌包时 packets、accepted 与 dropped 三个计数必须与调用次数完全一致，不能出现同一包被接受两次或计数丢失；票据只能使用一次，纪元推进、活动路径切换与宽限期写入必须在同一临界区内完成。迁移次数、候选路径数、宽限时长与乱序窗口都是硬顶，超限拒绝而不是覆盖旧值；快照恢复后票据序号、纪元水位与去重状态都要在，且不与恢复前的对象共享结构。

验收：`python3 -m pytest -q tests` 的 15 条必须全部通过；不得修改测试、跳过或按固定输入写死；并发与重启恢复场景自行验证；只做本地实现，不涉及上游提交。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- name: batch48-py-safety-a-17
- domain: 安全
- sub: 网络与通信安全
- type: Bugfix
- difficulty: 困难
- module: conn_migration_guard
- typeName: ConnMigrationGuard
- file: conn_migration_guard.py
