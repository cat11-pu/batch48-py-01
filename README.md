# batch48-py-game-a-15

## 题面

大世界分区合服后模拟线程的负载曲线变得很难看：王城门口的玩家实体每 tick 都被算一遍，远处的采集点和野怪也在同一帧醒来，服务器单帧耗时直接翻倍；有玩家报告靠近城墙时同一个守卫的动画在两档刷新率之间来回跳，走一步抖三次；事件系统唤醒了休眠的营火，下一帧它又被距离规则按回休眠，火苗闪一下就灭；预算不够时总有几个实体连续几十帧轮不到，等它终于更新时角色已经在原地卡了很久；跨区域传送后实体的档位还按旧区域的归属计算，恢复存档后更是长时间停留在过期档位。范围是 gsb/batch48-py-game-a-15 的 sim_lod_guard.py，类 SimLodGuard，负责距离分档与滞回、每 tick 更新预算、最久未更新排序与档位存档恢复。

公开接口：SimLodGuard(*, budget=8, hysteresis=20.0)；register_entity(entity_id, *, distance, region="A") -> dict；update_distance(entity_id, distance) -> dict；set_tier(entity_id, tier) -> dict；move_region(entity_id, region) -> dict；wake(entity_id, *, urgent=True) -> dict；tick() -> dict；tier_of(entity_id) -> str；last_updated(entity_id) -> int | None；entities() -> dict；budget() -> int；stats() -> dict；reset_stats() -> None；snapshot() -> dict；restore(state) -> None。拒绝统一抛 SimError(code, detail)。

故障与边界：一、档位固定为 each_tick、every_2、every_8、sleep，距离小于 100 为 each_tick，小于 300 为 every_2，小于 800 为 every_8，其余 sleep，注册按原始距离直接分档并返回 entity、tier、region；二、重复注册抛 SIM_DUPLICATE_ENTITY 且不改变原档位与距离，未知实体的更新、切换、查询统一抛 SIM_UNKNOWN_ENTITY；三、update_distance 按滞回切换：升到更粗的档要求距离大于当前档上界加 hysteresis，回到更细的档要求距离小于当前档下界减 hysteresis，同档只更新距离，返回 entity、tier、changed，允许一次跨多档；四、set_tier 显式切换并计入 tier_changes，返回 entity 与 tier，非法档名抛 SIM_TIER_INVALID；五、move_region 按保存的原始距离重新分档、不套用滞回，返回 entity、region、tier，其造成的切换同样计入 tier_changes；六、tick 每调用一次帧号加一并返回 tick、updated、deferred，更新预算为 budget，超出预算的合格实体进入 deferred 而不是被丢弃；七、each_tick 每帧合格，every_2 在偶数帧合格，every_8 在帧号为 8 的倍数时合格，sleep 永不参与；八、同一帧内先按档位优先级 each_tick、every_2、every_8 排序，同档按距上次更新更久优先，再同则按实体名升序；九、last_updated 在实体从未更新时返回 None，更新后返回最后一次被选中的帧号，entities 返回每个实体的 tier、distance、region、last_updated；十、休眠实体被紧急事件唤醒时立即升档且不套用滞回，同一帧内先降档又因事件升档只执行一次更新并以升档为准；十一、stats 固定返回 ticks、updates、deferred、tier_changes 四个整数，注册与首次分档不计入 tier_changes，reset_stats 归零；十二、snapshot 深拷贝实体表、距离、区域、帧号与统计，顶层 version 当前为 1，restore 后继续 tick 的结果与恢复前一致，结构非法抛 SIM_SNAPSHOT_INVALID 且不破坏原状态。

硬约束：register_entity、update_distance、set_tier、move_region、tick 全程持锁，八路并发切换档位时 tier_changes 必须等于实际切换次数；快照恢复后档位与预算不得停留在过期状态。验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发与恢复场景自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 游戏开发
- type: Bugfix
- difficulty: 地狱
- module: sim_lod_guard
- typeName: SimLodGuard
