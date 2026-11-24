# batch48-py-game-a-28

## 题面

周常首领的掉落清单连续两周被玩家贴出来质疑：写着权重为零的诅咒装备照样掉；每次必给的保底金币偶尔消失；同一次击杀被战报系统补算两遍，背包里直接多出一份；三个首领共用同一个保底计数，打完一个另一个的保底被提前触发；一次击杀的掉落条目多到刷屏，抽取次数没有上限；空掉落表还会返回空值，调用方判空时直接报错。值班的经济策划把三张表的配置贴在一起比对，发现必掉、权重与保底三套逻辑互相挤占预算，同一笔击杀的流水号也没有统一。范围是 gsb/batch48-py-game-a-28 下的 drop_table_roller.py，类 DropRoller，负责掉落表登记、加权抽取、必掉组、保底计数、单次结算预算与快照恢复。

公开接口：`DropRoller(*, seed: int = 1337, max_rolls_per_kill: int = 6, pity_threshold: int = 8)`；`RegisterTable(table_id, spec) -> dict`；`Roll(table_id, kill_context, rng=None) -> list`；`Guarantee(table_id, key) -> bool`；`Pity(table_id, key) -> int`；`Budget(table_id) -> int`；`Settled(table_id, kill_id) -> bool`；`ResetPity(table_id, key) -> int`；`Snapshot() -> dict`；`Restore(state) -> None`；`Stats() -> dict`。拒绝统一抛 DropTableError(code, detail)，抽取预算与保底阈值都取整数。

故障与边界：一、表结构含 guaranteed、pools 与 pity，guaranteed 条目为 key、min、max，pools 条目为 name、picks、entries，entries 为 key、weight、min、max，pity 为 key、threshold；二、权重为零的条目永不入池，负数权重、picks 大于 0 却没有任何正权重条目、结构缺字段都抛 DROP_TABLE_INVALID，重复登记抛 DROP_TABLE_ALREADY_REGISTERED；三、Budget 只统计各池 picks 之和，必掉组不占抽取次数预算，总 picks 超过 max_rolls_per_kill 时登记失败抛 DROP_BUDGET_EXCEEDED，失败后该表不可见；四、必掉组每次结算逐条产出，数量在 min 到 max 之间取值，source 为 guaranteed，顺序排在普通池之前；五、普通池按权重抽取 picks 次，source 为 pool:池名，条目数量在 min 到 max 之间取值，多个池按登记顺序排列；六、保底以表与保底键为维度计数，每完成一次结算加一，达到阈值的那次结算追加一条 source 为 pity 的掉落，触发后计数按规则清零或递减，不能只增不减；七、同一 table_id 与 kill_id 只结算一次，重复调用返回同一份结果的深拷贝，调用方改动返回值不得污染缓存，kills 不增加；八、kill_context 缺少非空 kill_id 抛 DROP_KILL_INVALID；九、空表允许登记，结算返回空列表而不是空值，并照常记账；十、Guarantee 对必掉键返回 True，对普通池键返回 False；十一、Pity 与 ResetPity 只接受该表的保底键，ResetPity 返回重置前的计数，未知表与未知保底键分别抛 DROP_TABLE_UNKNOWN、DROP_PITY_UNKNOWN；十二、快照保存表定义、保底计数、已结算击杀与内部随机源状态，恢复后同一击杀重放结果一致，结构非法抛 DROP_SNAPSHOT_INVALID 且原状态不变。Roll 的每条掉落含 key、amount、source；Settled 返回该击杀是否已结算；ResetPity 返回重置前的计数。Stats 返回 kills、drops、guaranteed、pity_triggers 四个整数。

硬约束：同一击杀并发结算只产出一份掉落，全部接口共用一把可重入锁；抽取次数受预算约束，禁止无界循环与整表重抽；快照恢复后随机源与保底计数必须与快照时刻一致，重启不改变下一次结算结果。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；保底触发与并发结算自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 游戏开发
- type: Bugfix
- module: drop_table_roller
- typeName: DropRoller
