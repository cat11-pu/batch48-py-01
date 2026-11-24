# batch48-py-game-a-30

## 题面

新副本开放的第二天，运营就收到了排班事故报告：显示满员的房间还能把人放进去；同一个固定队连点两次申请，占用了两个实例，队员被拆到两边；重置时间按每个实例的创建时刻各算各的，同一张地图的刷新时间越飘越远；有人退出后槽位一直不回收，队列排到几百号；刚退出的玩家立刻重进，冷却完全不起作用；实例被回收之后，名单里的玩家还能走进去；并发申请时同一个空位被卖给了两支队伍。值班的运维把申请日志按毫秒排开，发现槽位计数、队伍绑定与冷却时间分别记在三个地方，谁也没有对过账。范围是 gsb/batch48-py-game-a-30 下的 instance_slot_allocator.py，类 InstanceSlotAllocator，负责实例池登记、槽位分配、队伍绑定、进出、重置与回收。

公开接口：`InstanceSlotAllocator(*, reclaim_cooldown_ms: int = 60000, reentry_cooldown_ms: int = 30000)`；`RegisterDungeon(dungeon_id, *, capacity, max_slots, reset_every_ms, anchor_ms=0) -> dict`；`Acquire(dungeon_id, party, now) -> dict`；`Enter(instance_id, player, now) -> dict`；`Leave(instance_id, player, now=None) -> dict`；`Reset(instance_id) -> dict`；`Sweep(now) -> list`；`State(instance_id) -> dict`；`Cooldown(party, now=None) -> int`；`Snapshot() -> dict`；`Restore(state) -> None`；`Stats() -> dict`。拒绝统一抛 InstanceError(code, detail)，时间一律毫秒整数。

故障与边界：一、实例标识为副本名加井号加序号，序号从 1 递增，State 返回 instance_id、dungeon_id、party、capacity、members、state、created_at、reset_at；二、容量、槽位与重置周期必须为正整数，重复登记抛 INSTANCE_DUNGEON_DEFINED，非法配置抛 INSTANCE_DUNGEON_INVALID，未知副本抛 INSTANCE_UNKNOWN_DUNGEON；三、队伍为空、含重复成员或人数超过 capacity 分别抛 INSTANCE_PARTY_INVALID、INSTANCE_PARTY_TOO_LARGE；四、同一队伍已有未关闭实例时再次申请返回原实例，acquired 不增加；五、满员判定先于成员归属校验，容量已满时任何进入请求抛 INSTANCE_FULL，非本队成员在未满时抛 INSTANCE_FOREIGN_PLAYER；六、重复进入幂等，返回同一成员名单且 entered 只计一次，成员顺序按首次进入排列；七、离开移除成员并记录该玩家的重进冷却，非成员离开抛 INSTANCE_NOT_MEMBER，冷却未到期进入抛 INSTANCE_REENTRY_COOLDOWN，到期时刻允许进入；八、重置清空成员、保留队伍绑定与容量，重置不产生重进冷却；九、重置周期按固定锚点整点推进，reset_at = anchor + ((now - anchor) // period + 1) * period，不能从实例创建时刻起算；十、Sweep 关闭已到期实例并返回按标识排序的列表，被关闭实例的 State 为 closed，槽位释放；十一、队伍全员离开后实例进入待回收而不是立即销毁，回收冷却期内保留绑定，Acquire 命中冷却抛 INSTANCE_COOLDOWN_ACTIVE，Cooldown 返回剩余毫秒；十二、快照保存实例、绑定队伍、成员、冷却与下次重置时刻，恢复后冷却与重置不丢失，结构非法抛 INSTANCE_SNAPSHOT_INVALID 且原状态不变。RegisterDungeon 返回 dungeon_id、capacity、max_slots；Acquire 与 State 返回同样的实例字段；Enter 返回 instance_id、player、members、capacity；Leave 返回 instance_id、player、members、state；Reset 返回 State 结构；Sweep 返回按标识排序的实例列表；Cooldown 返回剩余毫秒。Stats 返回 acquired、entered、left、reclaimed 四个整数。

硬约束：同一副本并发 Acquire 不得超过 max_slots，槽位检查与占用在同一把可重入锁内完成；同一队伍不得同时绑定两个实例；快照恢复后冷却与重置锚点必须继续生效，重启不得把冷却清零。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；整点重置与并发超卖自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 游戏开发
- type: Bugfix
- module: instance_slot_allocator
- typeName: InstanceSlotAllocator
