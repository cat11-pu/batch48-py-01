# batch48-py-game-c-03

## 题面

直播推流上线两个月，主播一进弱网码率就掉到 200kbps 再也升不回来，画面糊成一片；网络恢复后第一秒编码器又猛推一大串包，把发送队列和中间设备一起打满，观众端开始连续丢帧；更糟的是过载时关键帧被挤掉，新进房的观众一直黑屏等下一个关键帧。运维翻发送端日志，只看到一个每 200 毫秒跑一次的调速循环，升档没有任何门槛，降档靠人工改配置，令牌桶容量按固定字节算，重启后桶又满着复活，第一秒必然突发；测试环境的发送队列只有 128KB，关键帧正好赶上一串小包时按到达顺序一刀切，主播侧看起来就是画面一顿一顿。范围是 `gsb/batch48-py-game-c-03` 下的 `pacer_rate_guard.py`，类 `PacerRateGuard`，负责目标码率、发送节拍、队列过载与探测周期。

公开接口：`PacerRateGuard(*, min_bps=200000.0, max_bps=4000000.0, start_bps=800000.0, capacity_ms=100.0, max_queue_bytes=120000, clock=None)`；`enqueue(frame, *, now_ms) -> dict`；`tick(now_ms) -> dict`；`on_cycle(no_congestion) -> dict`；`report_loss(loss_ratio, *, now_ms=None) -> dict`；`target_bps() -> float`；`tokens_bytes() -> float`；`mode() -> str`；`queue_bytes() -> int`；`queue_depth() -> int`；`snapshot() -> dict`；`restore(state) -> None`；`stats() -> dict`；`reset_stats() -> None`。拒绝统一抛 `PacerError(code, detail)`，code 取 PACER_INVALID_FRAME、PACER_INVALID_LOSS、PACER_SNAPSHOT_INVALID。

故障与边界：一、frame 必须是含 seq、bytes、keyframe、droppable 的字典，seq 为非负整数、bytes 为正整数，否则 PACER_INVALID_FRAME；重复 seq 拒绝并计入 duplicates。二、目标码率夹在 min_bps 与 max_bps 之间，初始为 start_bps。三、on_cycle 连续两个干净周期才升 5% 且升后重新计数，有拥塞的周期立即降 15% 并进入探测周期。四、探测周期持续两个干净周期，期间不升档，结束后回到 steady。五、report_loss 的丢包率必须在 0 到 1 之间，否则 PACER_INVALID_LOSS；超过 0.10 立即降 15% 并把升档计数清零，恰好 0.10 不调整。六、令牌桶按目标码率折算字节速率补充，容量等于目标码率乘 capacity_ms 除以 8000，空闲补充不得超过容量。七、tick 按队列顺序取整帧发送，令牌不足即停，返回 sent、bytes_sent、tokens、target_bps、mode。八、排队字节超过 max_queue_bytes 时从最旧的可丢弃帧开始丢，直到能放下新帧；没有可丢帧时返回 accepted 为 False、reason 为 queue_full。九、关键帧与不可丢弃帧不参与丢弃，过载期间也必须保留在队列里等待发送。十、单次拥塞之后至少两个周期内不得连续升档，升档计数只能逐个周期累加。十一、snapshot 深拷贝 target_bps、tokens、mode 与 queue，restore 结构非法抛 PACER_SNAPSHOT_INVALID 且原状态不变，恢复后的令牌不得超过容量上限。十二、stats 固定返回 enqueued、duplicates、rejected_frames、dropped_frames、sent_frames、bytes_sent、cycles_up、cycles_down、loss_reports 九个整数，reset_stats 归零。

硬约束：enqueue 与 tick 共用一把可重入锁，四路并发各入队 25 帧后 enqueued 必须正好等于 100；队列字节按 max_queue_bytes 封顶，禁止无界增长；令牌桶补充按毫秒计时，禁止按 tick 次数估算码率；重启恢复后首个 tick 不得一次性突发；统计里的字节与帧数必须按实际发送累加，不得按入队数量估算。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发与重启恢复场景自行验证；只做本地实现，不涉及上游提交。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- name: batch48-py-game-c-03
- domain: 游戏、图形与多媒体
- sub: 音视频处理
- type: Bugfix
- difficulty: 困难
- module: pacer_rate_guard
- typeName: PacerRateGuard
