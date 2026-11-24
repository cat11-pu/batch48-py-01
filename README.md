# GSB 任务说明

## 题面

新赛季的操作队列上线后，排位服每帧只肯放固定预算，客服收到两类极端反馈：高延迟玩家连点三次技能，前两次进了队列，第三次报错说队列已满，结果他自己的第二个指令也没执行；一个主播疯狂刷移动指令，队友的常规指令连续十几帧都没被选中，最后全部过期；团战里治疗指令明明比移动更急，却排在普通指令后面被预算卡掉；还有玩家发现指令被取消之后同一序号可以重新提交，服务重启后待处理队列整个清空。运维日志里，预算用量只能看到一个总数，说不清分给了哪个玩家；队列长度也没有上限，异常客户端可以把内存打满。

范围是 gsb/batch48-py-game-a-10 的 input_budget_arbiter.py，类 InputBudgetArbiter，负责指令入队与去重、过期清理、每帧预算仲裁、跨玩家公平轮转、取消与快照恢复。

公开接口：InputBudgetArbiter(*, budget=8, player_limit=8, queue_limit=32, default_ttl=500, max_cost=4, clock=None)；set_budget(budget, *, frame=None) -> dict；budget_of() -> int；submit(player, *, seq, entity, command, priority="normal", cost=1, ttl=None, frame=0, now=None) -> dict；pending(*, player=None) -> dict；item_of(player, *, seq) -> dict | None；cancel(player, *, seq) -> dict；expire(*, now=None) -> int；arbitrate(*, frame, now=None) -> dict；stats() -> dict；reset_stats() -> None；snapshot() -> dict；restore(state) -> None。拒绝统一抛 InputError(code, detail)。

故障与边界：一、指令按 (玩家, 客户端序号) 去重，内容完全一致返回 duplicate 且不重复入队，任一字段不同按冲突拒绝并保留首次入队内容；二、玩家名、序号、实体、指令名必须非空，优先级只允许 urgent、normal、bulk，单条 cost 必须在一到 max_cost 之间，ttl 必须为正整数；三、每玩家积压达到 player_limit 拒绝，全局队列达到 queue_limit 拒绝，拒绝原因必须能区分是哪个上限；四、过期指令在仲裁前统一丢弃，到达过期时刻即失效，过期与取消都要计入统计；五、紧急档先分配预算，可以抢占常规与批量，紧急指令装不下时保留在队列而不是截断；六、常规与批量按最久未被服务的玩家优先轮转，被抢占的常规指令要保留原入队时刻，下一帧按老化继续参与，不能重新排队；七、同一玩家同一帧提交多条互斥指令时只保留优先级最高的一条并记录丢弃；八、同一实体的指令必须保持入队顺序，更早的同实体指令还没入选时，更晚的指令不得越过它执行；九、预算按 cost 扣减，装不下的指令整条留在队列，不得部分执行，预算为零时不得静默清空队列，指令保留到过期或被取消；十、cancel 只处理待处理指令，未知序号直接拒绝，取消后同一序号可以重新提交；十一、pending 与 item_of 要能按玩家和序号查询队列内容，预算用量可解释到具体玩家；十二、snapshot 深拷贝预算、队列、服务顺序与统计，restore 后继续仲裁结果一致，结构非法拒绝且不破坏原状态。

硬约束：提交、取消、过期与仲裁全程持锁，多线程同时提交同一玩家同一序号只能入队一次，其余返回 duplicate；快照跨进程恢复后待处理队列与预算不丢；玩家积压、全局队列与单条 cost 都有硬顶，超出必须拒绝并给出明确原因码。

验收：`python3 -m pytest -q tests` 的 15 条测试全部通过；不得修改测试、跳过或按固定输入写死；并发与重启场景自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 游戏开发
- type: Bugfix
- difficulty: 地狱
