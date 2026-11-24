# batch48-py-game-a-35

## 题面

采集节点改版后矿区矛盾集中爆发：同一个矿点被两名玩家同时开采，采出来的矿石比节点储量还多，玩家挂机占着节点不放手，节点明明刷新了却一直显示枯竭，采集到一半退出时背包数量对不上，节点被策划删掉后旧声明还能继续用；运营每天按玩家背包核对产出，差额都要人工补发，矿区一天要处理上千次采集，任何一次超采都会直接变成工单。问题集中在 gsb/batch48-py-game-a-35 的 resource_node_registry.py，类 NodeRegistry 负责登记节点、占用声明、按储量结算、超时回收与刷新。

公开接口：NodeRegistry(*, claim_lease=30, default_respawn=60)；register_node(node_id, *, capacity=100, respawn_seconds=None, now=0) -> dict；remove_node(node_id) -> dict；node(node_id) -> dict；claim(node_id, player, now=0) -> dict；harvest(claim_id, amount, now=0) -> dict；release(claim_id) -> dict；respawn(node_id, now=0) -> dict；available(node_id, now=0) -> bool；inventory(player) -> int；claims(player=None) -> list；snapshot() -> dict；restore(state) -> None。拒绝统一抛 NodeError(code, detail)；claim 返回 claim_id、node_id、player、remaining、expires_at 与 produced；register_node 与 node 返回 node_id、capacity、remaining、respawn_seconds、respawn_at、depleted 与 occupier，未给 respawn_seconds 时取 default_respawn；harvest 返回 claim_id、harvested、remaining 与 depleted，release 返回 claim_id 与 released，remove_node 返回 node_id 与 removed。

故障与边界：一、同一节点同时只允许一个有效占用，第二人占用抛 NODE_OCCUPIED；二、单次采集超过剩余储量时按剩余量结算，不得采出负数或超过储量，剩余为零后再采抛 CLAIM_EMPTY，结算量以节点当前剩余为准；三、未刷新的枯竭节点不能占用，抛 NODE_DEPLETED；四、占用超过租约时长后在下次访问时回收，过期声明再采集抛 CLAIM_EXPIRED，回收后他人可以重新占用，expires_at 为占用时刻加 claim_lease；五、刷新锚点固定在节点枯竭那一刻，之后每次采集不得把刷新时间往后推，到点后按原储量恢复；六、采集中途放弃时已产出部分按实际产量结算，不能整份回滚也不能按满额发放，放弃后节点占用立即释放；七、节点被删除后原有声明立即失效，再采集抛 CLAIM_INVALID，已释放的声明再操作同样抛 CLAIM_INVALID；八、采集数量为零或负数抛 CLAIM_INVALID_AMOUNT，未知节点与未知声明分别抛 NODE_UNKNOWN 与 CLAIM_UNKNOWN，容量非正抛 NODE_INVALID，重复登记抛 NODE_DUPLICATE；九、刷新只由时间或显式调用触发，不能因为查询就把时间提前，查询不消耗储量；十、快照返回 version 为 1 的字典，含 nodes、claims 与 inventories 三段，结构非法抛 NODE_SNAPSHOT_INVALID 且原状态不变。

硬约束：同一节点的并发采集必须串行扣减储量，总量恰好等于储量、不得超采，任何线程都读不到负数余量；重启恢复后超时占用被回收、刷新时间不提前，恢复后继续采集的产量与中断前衔接；声明条数与单次采集量受上限约束，不允许无界增长，采集与释放交错时储量只减不增。

验收：python3 -m pytest -q tests 的 15 条测试必须全过，不得改测试、跳过或按固定输入写死；中途放弃与刷新锚点自行验证；只做本地实现，接口名与错误码保持上面列出的写法，刷新时刻与占用者在快照中保持一致。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 游戏开发
- type: Bugfix
- difficulty: 困难
- module: resource_node_registry
- typeName: NodeRegistry
