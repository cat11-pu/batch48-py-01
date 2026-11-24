# batch48-py-game-a-01

## 题面

周末联赛的观战通道上线后，客服工单集中在一个房间：被换下场的首发还能继续丢技能，替补排在第二位的反而先被换上去，观战席上的人点技能没有被拦，掉线和主动下场同时发生时同一个席位被换了两遍，房间结算之后还有替补被塞进场上；另一个房间则是观战席满员后新观众收到的报错说房间不存在，编排服务重启后所有人的席位和替补顺序全乱了。

客服还能复现出同一玩家同时挂在首发席和观战席上，回放记录对不上人；替补席第二位明明先报名，换上去的却是最后一位。

范围是 gsb/batch48-py-game-a-01 的 spectator_bench_orchestrator.py，类 SpectatorBenchOrchestrator，负责首发席、替补席与观战席的编排、权限判定和实体控制权迁移。

公开接口：SpectatorBenchOrchestrator(*, active_slots=5, spectator_limit=8, bench_limit=4, clock=None)；assign_active(seat: int, player: str) -> dict；join_spectator(player: str) -> dict；join_bench(player: str) -> dict；upgrade_spectator(player: str) -> dict；promote(player: str, seat: int, *, reason: str = "voluntary", expect_occupant=None) -> dict；demote(player: str, *, reason: str = "timeout") -> dict；swap_seat(player_a: str, player_b: str) -> dict；set_permission(player: str, allowed: bool) -> dict；can_operate(player: str) -> bool；另有 leave、seat_state、roster、settle_match、stats、reset_stats、snapshot、restore。拒绝统一抛 SpectatorError(code, detail)。

故障与边界：一、席号必须落在 1 到 active_slots，占用中的席位再分配报 SPECTATOR_SEAT_OCCUPIED，同一玩家不能同时占两种席；二、观战席只读，给观战者开操作权限报 SPECTATOR_READ_ONLY，观战席与替补席各自按上限拒绝；三、替补按加入顺序排队，promote 只接受替补席上的玩家，观战者要先转替补，否则报 SPECTATOR_BENCH_REQUIRED；四、promote 必须是原子替换，目标席位被占时原首发回到替补席尾部并记录原因，席位为空报 SPECTATOR_SEAT_EMPTY；五、demote 只对首发生效，替补席满时拒绝且不得把玩家从首发席摘掉；六、swap_seat 只在两名首发之间交换席号，同一玩家或非首发都拒绝；七、leave 要按角色释放对应席位并把替补队列压实；八、结算后房间冻结，再结算或再分配席位都报 SPECTATOR_MATCH_SETTLED；九、snapshot 必须是深拷贝，restore 结构非法报 SPECTATOR_SNAPSHOT_INVALID 且原状态不变；十、同一席位的重复替换请求只能有一个真正生效，另一个返回 already_processed 而不是二次换人；十一、对局已结算时不得再把替补塞进场；十二、观战者转替补后原观战席位必须释放，不能一人占两席。

硬约束：同一席位的并发替换只允许一个成功，其余请求返回已处理，不能出现同一玩家同时挂在两个席位；promote、demote、swap_seat、leave 全程持锁，八路并发把不同替补换到不同席位时计数必须与调用次数一致；结算后房间整体只读，任何写操作都不得改动席位；快照恢复后首发席、替补顺序、观战席、权限与统计都要在，且不得与恢复前的对象共享结构；席位、观战与替补容量都是硬顶，超限拒绝而不是覆盖旧席位。

验收：`python3 -m pytest -q tests` 的 15 条必须全过；不得修改测试、跳过或按固定输入写死；并发替换与快照恢复场景自行验证；只做本地实现，不涉及上游提交。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- name: batch48-py-game-a-01
- domain: 游戏、图形与多媒体
- sub: 游戏开发
- type: Bugfix
- difficulty: 困难
- module: spectator_bench_orchestrator
- typeName: SpectatorBenchOrchestrator
