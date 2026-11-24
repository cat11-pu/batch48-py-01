# batch48-py-game-c-01

## 题面

会议室投屏连续三周被投诉：Wi-Fi 一抖画面就每两秒顿一下，运维把缓冲调深以后顿挫消失，延迟却越积越多，半小时后讲解人的口型和手势明显对不上，客服收到的录屏里画面先追后等，同一场会议里声音和字幕始终独立于画面；有人手动快进回看，关键帧被跳过，整屏绿块要等下一个关键帧才恢复。现场排查发现抖动量测窗口取小了，缓冲深度和目标延迟各算各的，两个数一直在打架。日志里还看到序号在 65535 附近翻回零之后，播放器把整段画面当成未来数据越积越多，重启之后播放时刻又跳回原点。范围是 `gsb/batch48-py-game-c-01` 下的 `rtp_playout_guard.py`，类 `RtpPlayoutGuard`，按流维护到达抖动估计、目标延迟、序号展开与播放队列。

公开接口：`RtpPlayoutGuard(*, min_delay_ms=40.0, max_delay_ms=400.0, jitter_factor=3.0, step_ms=20.0, frame_ms=20.0, capacity=256, jitter_window=10, drop_after_ms=2000.0, clock=None)`；`push(seq, arrival_ms, timestamp_ms=None, *, keyframe=False, ref=False) -> dict`；`tick(now_ms) -> dict`；`target_delay_ms() -> float`；`jitter_ms() -> float`；`playout_ms() -> float`；`queue_depth() -> int`；`queued_seqs() -> list[int]`；`snapshot() -> dict`；`restore(state) -> None`；`stats() -> dict`；`reset_stats() -> None`。拒绝统一抛 `PlayoutError(code, detail)`，code 取 PLAYOUT_INVALID_SEQ、PLAYOUT_BUFFER_FULL、PLAYOUT_SNAPSHOT_INVALID。

故障与边界：一、seq 必须是 0 到 65535 的整数，否则 PLAYOUT_INVALID_SEQ，detail 带原值；timestamp_ms 缺省时按 seq 乘 20 毫秒推算。二、16 位序号先展开成连续整数再入队，重复的展开序号拒绝并计入 duplicates，返回 accepted 为 False、reason 为 duplicate。三、播放时刻已经越过的包算过期，拒绝并计入 late，reason 为 late。四、队列容量固定，满时普通包抛 PLAYOUT_BUFFER_FULL，detail 带 capacity 与 queue_depth。五、抖动估计取最近 jitter_window 个已接收包的到达时刻减时间戳之差的极差，样本不足两个时为 0。六、目标延迟等于抖动估计乘 jitter_factor，夹在上下限之间，初始取下限；向上每次 tick 最多走 step_ms，只有队列为空时才向下回落。七、tick 首次把播放时刻设为最早包时间戳加目标延迟且不推进，之后按经过时间乘速度推进，跨 tick 严格单调。八、积压超过目标两帧走 1.05 快放，不足一帧走 0.95 慢放，其余 1.0；速度只按 tick 开始时的积压判定，同一次 tick 内不改口；快放只能跳过多余的非关键帧且非参考帧，关键帧与参考帧不得丢弃。九、乱序到达的包按展开后的时间戳排队，播放列表按时间戳顺序给出，不按到达顺序。十、tick 返回 played、speed、target_delay_ms、playout_ms、dropped 五个字段。十一、snapshot 深拷贝 target_ms、playout_ms、queue 与 last_seq，restore 结构非法抛 PLAYOUT_SNAPSHOT_INVALID 且原状态不变。十二、stats 固定返回 accepted、duplicates、late、rejected、played、fast_forward 六个整数，reset_stats 全部归零。

硬约束：push 与 tick 共用一把可重入锁，四路并发各推 40 个不同序号的包时 accepted 必须正好等于 160；队列按容量封顶，禁止无界增长；同一时间戳的包按展开序号先后处理，不得重复播放；快照恢复后的播放时刻不得回退到已播放区间。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发与重启恢复场景自行验证；只做本地实现，不涉及上游提交。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- name: batch48-py-game-c-01
- domain: 游戏、图形与多媒体
- sub: 音视频处理
- type: Bugfix
- difficulty: 困难
- module: rtp_playout_guard
- typeName: RtpPlayoutGuard
