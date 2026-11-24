# batch48-py-game-a-02

## 题面

排位匹配成功之后到真正进对局之间有一段就绪确认，这周线上连续爆单：明明五个人都点了接受，进房却提示确认已关闭；有人点了拒绝，冷却期没过又重新排进同一场；确认窗口超时后，被踢的人立刻收到补位邀请，可同队的两名玩家被拆到了两边；同一个玩家在一场还没结束时就出现在另一场的确认列表里；还有房间把没点过接受的人算成已接受；确认服务重启后所有未答票据消失，接受过的记录也不算数了。客服按 check_id 复盘时发现票据条数对不上，统计里的接受数和实际点击次数差了一截。

范围是 gsb/batch48-py-game-a-02 的 ready_check_coordinator.py，类 ReadyCheckCoordinator，负责建票据、收集接受与拒绝、按模式窗口计时、超时判拒绝，以及给拒绝者记重排冷却。

公开接口：ReadyCheckCoordinator(*, ticket_limit=64, player_limit=512, mode_windows=None, default_window=5, penalty_ticks=3, clock=None)；window_for(mode: str) -> int；open_check(check_id: str, *, mode: str, players) -> dict；submit_ready(check_id: str, player: str, *, accept: bool, tick: int = 0) -> dict；tick(check_id: str, *, ticks: int = 1) -> dict；close_check(check_id: str) -> dict；disconnect(check_id: str, player: str) -> dict；status(check_id: str) -> dict；ticket_for(player: str) -> dict；requeue_penalty(player: str) -> dict；stats()、reset_stats()、snapshot()、restore(state)。拒绝统一抛 ReadyCheckError(code, detail)。

故障与边界：一、票据数与玩家索引都有硬顶，超了分别报 READY_TICKET_CAPACITY 与 READY_PLAYER_CAPACITY，关单后索引必须释放；二、同一 check_id 只能开一次，players 里重复玩家报 READY_DUPLICATE_PLAYER，空名字或缺 team 报 READY_INVALID_PLAYER，票据顺序按报名顺序保留；三、同一票据重复提交以第一次结果为准，第二次返回 duplicate 且不再计数，也不推进计时；四、窗口按对局模式取，未知模式用 default_window，空模式名报 READY_INVALID_MODE；五、超过窗口的提交报 READY_TOO_LATE，tick 步长必须为正；六、tick 推进到窗口时未答票据统一记 decline、原因记为 timeout，tick 要回报剩余未答数；七、全员接受才出 ready 结论，有人拒绝出 partial，未答满时不得提前下结论，verdict 在关闭前后保持一致；八、close_check 关闭后迟到 accept 必须报 READY_CHECK_CLOSED，已关闭的对局不能被复活；九、close 的汇总要给出 accepted、declined、priority_requeue 与 penalized，priority_requeue 顺序与 accepted 一致，拒绝者按 penalty_ticks 累计冷却；十、同一玩家已在未关闭的票据里时，再开新票据报 READY_PLAYER_BUSY；十一、接受者补位时要保留原队伍分组，不能拆散小队；十二、全员接受之后有人立刻断线按断线处理，不得把已确认回滚成未确认。

硬约束：open_check、submit_ready、close_check 全程持锁，十二路并发对同一张票据提交接受时 accepts 必须恰好等于成功次数，不能丢计数也不能重复入账；拒绝冷却按账号累计，关单后重新开票不得清零；关单与玩家索引更新必须原子，索引里不能残留已关闭票据的玩家；快照必须覆盖票据、状态、冷却与统计，恢复后未答的票据、拒绝的原因和冷却值都还在，且不得与恢复前的对象共享结构。

验收：`python3 -m pytest -q tests` 的 15 条必须全过；不得修改测试、跳过或按固定输入写死；并发提交与快照恢复自行验证；只做本地实现，不涉及上游提交。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- name: batch48-py-game-a-02
- domain: 游戏、图形与多媒体
- sub: 游戏开发
- type: Bugfix
- difficulty: 困难
- module: ready_check_coordinator
- typeName: ReadyCheckCoordinator
