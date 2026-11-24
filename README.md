# batch48-py-game-a-20

## 题面

赛季排队系统换成分段加载后，开局阶段频繁卡住：八个人里七个早就加载完，界面停在等待其他玩家整整二十秒，最后还得手动解散重排；有一次超时托管已经接管，一个玩家恰好在判定那一帧把进度推到位，系统既算了托管又保留了等待，大厅显示人数比实际多一个；读条期间掉线的玩家被立刻踢出，位置空了没人补，等宽限期过了才发现这局一直是七打八；补位进来的玩家从当前帧续算，开局头两秒角色瞬移，回放里能看到他先出现在终点又退回去；放行之后有人掉线，屏障又被打回等待状态，整局重新加载；就绪状态只存在内存里，排队进程重启后所有人回到未就绪。范围是 gsb/batch48-py-game-a-20 的 match_barrier_guard.py，类 MatchBarrierGuard，负责加载阶段门槛、超时托管、宽限期补位与放行基线。

公开接口：MatchBarrierGuard(*, threshold="baseline", timeout_ticks=10, grace_ticks=5, max_players=8)；join(player, *, slot=None, tick=0) -> dict；report_ready(player, stage, *, tick=0) -> dict；barrier(tick) -> dict；drop(player, *, tick) -> dict；substitute(player, *, slot=None, tick=0) -> dict；advance(tick) -> dict；state() -> str；released_at() -> int | None；deadline() -> int；short_handed() -> list；players() -> dict；stats() -> dict；reset_stats() -> None；snapshot() -> dict；restore(state) -> None。拒绝统一抛 MatchBarrierError(code)。

要修的点：一、阶段固定为 assets、scene、baseline，threshold 必须是其中之一；join 按到达顺序分配 slot 并返回 player、slot、stage、ready、state，重复玩家抛 MATCH_DUPLICATE_PLAYER，位子满了抛 MATCH_FULL，玩家名为空抛 MATCH_INVALID_ARGUMENT；二、report_ready 记录已到达的最高阶段，达到 threshold 即 ready，阶段只能前进，回退抛 MATCH_STAGE_REGRESSION，未知阶段抛 MATCH_INVALID_STAGE，重复上报同一阶段幂等；三、deadline 等于最早加入时刻加 timeout_ticks，barrier 在全员达门槛时立即放行，否则倒计时到点按托管接管仍未达标的玩家并把他们标为 hosted；四、barrier 返回 tick、released、waiting、hosted、released_at，未放行时 released_at 为空；五、放行是单向的，放行后 state 恒为 released、released_at 不再变化，再次调用 barrier 只回报原放行时刻，放行后再上报就绪或新玩家入座都抛 MATCH_ALREADY_RELEASED；六、放行前掉线直接把玩家移出等待集合并标记 left，其位置不再阻塞放行；七、放行后掉线进入宽限期，drop 返回 player、state 为 grace、deadline 为当前 tick 加 grace_ticks，此时不得回退屏障状态；八、advance 只处理宽限到期，超过 deadline 仍未补位的玩家标为 absent 并计入缺员，这局按缺员继续，宽限期内补位的玩家要拿放行时刻的权威基线，不能从当前 tick 直接续；九、substitute 只能在放行后的宽限期内补位，接管最早到期的宽限位并沿用其 slot，超出宽限抛 MATCH_GRACE_EXPIRED，没有开口的宽限抛 MATCH_INVALID_STATE；十、超时托管与玩家恰在边界完成加载的竞态以判定时刻的进度快照为准，不能两者都生效；十一、stats 固定返回 joins、reports、barriers、released、hosted、dropped、substituted、short_handed 八个整数，reset_stats 归零但不清放行状态；十二、snapshot 返回 version=1 的深拷贝，restore 后玩家表、放行状态、放行时刻、deadline 与统计一致，结构非法抛 MATCH_SNAPSHOT_INVALID 且不破坏当前状态。

硬约束：join、report_ready、barrier、drop、substitute、advance 与快照读写全程持同一把锁，八路并发上报与并发调用 barrier 下放行只发生一次，所有调用回报同一个 released_at；快照跨进程恢复后不得与恢复前的对象共享可变结构；玩家数受 max_players 硬顶。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发上报、超时托管与重启恢复自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 游戏开发
- type: Bugfix
- module: match_barrier_guard
- typeName: MatchBarrierGuard
