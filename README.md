# batch48-py-game-a-26

## 题面

钓鱼玩法在周末活动上线后投诉不断：咬钩提示已经消失，玩家仍然显示钓上鱼；同一台服务器上稀有鱼的出现概率一天之内涨落三次；同一名玩家抛出两根竿后两边读数互相覆盖；断线重连的玩家回到岸边，咬钩倒计时从头开始，鱼却还挂在钩上；两名玩家同时对着同一个钓点确认咬钩，双方都拿到了同一条鱼。结算流水里同一名玩家在同一条鱼上出现两条记录，一行标为落鱼、一行标为脱钩；值班同学把日志翻了三遍，发现倒计时、张力与耐久三份读数各记各的，谁也说不清哪份算数。范围是 gsb/batch48-py-game-a-26 下的 fishing_minigame_guard.py，类 FishingMinigameGuard，负责抛竿、咬钩计时、收线推进、张力控制、落鱼与脱钩结算。

公开接口：`FishingMinigameGuard(*, bite_window_ms: int = 1200, max_tension: int = 100, line_durability: int = 3, tables=None, clock=None)`；`Cast(player, spot, now) -> dict`；`Bite(player, now) -> dict`；`Reel(player, dt_ms) -> dict`；`SetTension(player, delta) -> dict`；`Land(player, now) -> dict`；`Escaped(player) -> dict`；`Release(player) -> dict`；`State(player) -> dict`；`SpeciesOdds(spot, now=None) -> dict`；`Snapshot() -> dict`；`Restore(state) -> None`；`Stats() -> dict`。拒绝统一抛 FishingError(code, detail)，code 取下列字符串；时间一律毫秒整数。

故障与边界：一、咬钩窗口从确认时刻起算，deadline = now + bite_window_ms，窗口结束即脱钩；收杆与窗口结束落在同一毫秒按窗口已关闭处理，只能产生一个结果；二、同一玩家已有活跃钓线时再次抛竿抛 FISH_LINE_BUSY，casts 不增加；三、同一咬钩重复确认返回同一 deadline，不得延长窗口，bites 只计一次；四、每 50 毫秒推进 1 点进度、每 100 毫秒累积 1 点张力，整除余数丢弃，dt_ms 非正抛 FISH_INTERVAL_INVALID；五、张力超过 max_tension 立即断线，result 为 snapped 并扣 1 点耐久，此后 Land 抛 FISH_ALREADY_SETTLED；六、进度不足 100 时 Land 抛 FISH_NOT_READY，状态保持 biting；七、Land 晚于 deadline 返回脱钩结果，cause 为 window_closed，耐久扣 1；八、Escaped 与 Release 各自幂等，重复调用返回同一结果，escaped 与 released 不重复计数，主动脱钩的 cause 为 player_escaped 且不扣耐久，抛竿后尚未咬钩也能直接收竿取消；九、同一钓点已有活跃钓线时其他玩家抛竿抛 FISH_SPOT_TAKEN，且不得改动第一位玩家的张力与耐久；十、落地返回 landed、species、weight_g（200 到 999 的整数）与 durability，species 必须取自该钓点配置表，落地不扣耐久；十一、SpeciesOdds 返回权重深拷贝，权重在构造时固定，传 now 不改变结果，调用方改动返回值不得影响内部权重，未知钓点抛 FISH_UNKNOWN_SPOT；十二、snapshot 为深拷贝，restore 结构非法抛 FISH_SNAPSHOT_INVALID 且原状态不变，快照中的计数与窗口必须一并恢复。

State 返回 player、state、spot、cast_id、tension、progress、durability、deadline；Bite 返回 state、bite_at、deadline、window_ms；Reel 与 SetTension 返回 progress、tension、durability、state、result；Land 返回 result、cause、species、weight_g、durability、progress；Release 返回 state、released、cast_id；Stats 返回 casts、bites、landed、escaped、released。内部状态以 cast_id 关联，同一 cast_id 只能结算一次，任何一次脱钩都要在结果里留下 cause。

硬约束：同一玩家同一时刻只允许一条活跃钓线，所有接口共用同一把可重入锁；咬钩确认、落地与脱钩在同一临界区内结算，并发调用只产生一次结果与一次计数；快照保存剩余窗口、张力、进度与耐久，恢复后倒计时继续而不是重置。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；同帧竞态与重连恢复自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 游戏开发
- type: Bugfix
- module: fishing_minigame_guard
- typeName: FishingMinigameGuard
