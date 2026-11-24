# batch48-py-game-c-02

## 题面

视频会议的长会一开就是两小时，客服工单里反复出现声音比画面慢半秒，用户形容像看译制片；切一次摄像头之后偏差又突然跳回零，前面攒下的漂移像没发生过；偶尔有参会人把手机锁屏再解锁，恢复后第一帧画面猛抖一下，之后声音又慢慢落后。抓包看 RTCP SR，音频流的时间戳走速和 NTP 墙钟并不一致，运营商侧 NTP 偶发回跳一次，参会端立刻把整段音频重排。现场调整过两次补偿系数，都是靠肉眼比对口型拍的，切换主时钟也没有任何确认，谁最后上报谁说了算；运维在会议室侧抓了两周日志才确认这是媒体时钟问题，不是网络丢包。范围是 `gsb/batch48-py-game-c-02` 下的 `av_drift_sync_guard.py`，类 `AvDriftSyncGuard`，负责 SR 映射、漂移估计、水位补偿与主时钟管理。

公开接口：`AvDriftSyncGuard(*, max_streams=8, max_samples=32, clamp_low=0.98, clamp_high=1.02, warn_ms=200.0, early_ms=40.0, late_ms=120.0, low_watermark_ms=60.0, high_watermark_ms=240.0, clock=None)`；`register_stream(stream_id, *, kind, clock_rate) -> dict`；`unregister_stream(stream_id) -> None`；`ingest_sr(stream_id, *, ntp_ms, rtp_ts) -> dict`；`drift_ms(stream_id) -> float`；`drift_ratio(stream_id) -> float`；`stream_info(stream_id) -> dict`；`primary_stream() -> str | None`；`request_primary(stream_id) -> dict`；`enqueue_audio(stream_id, watermark_ms, *, now_ms=None) -> dict`；`schedule_video(stream_id, pts_ms, *, now_ms) -> dict`；`snapshot() -> dict`；`restore(state) -> None`；`stats() -> dict`；`reset_stats() -> None`。拒绝统一抛 `SyncError(code, detail)`，code 取 SYNC_INVALID_STREAM、SYNC_DUPLICATE_STREAM、SYNC_STREAM_LIMIT、SYNC_UNKNOWN_STREAM、SYNC_INVALID_SR、SYNC_SNAPSHOT_INVALID。

故障与边界：一、kind 只接受 audio 与 video，clock_rate 必须为正整数，否则 SYNC_INVALID_STREAM；重复注册 SYNC_DUPLICATE_STREAM；超过 max_streams 报 SYNC_STREAM_LIMIT。二、第一个注册的流自动成为主时钟，之后注册的不改变主时钟；主时钟流被注销后置空，需要重新指定。三、ingest_sr 用第一条 SR 建立 ntp 与 rtp 的基准映射，返回 baseline 为 True、漂移 0、比例 1。四、后续 SR 的 ntp 必须严格递增、rtp 不得回退，否则 SYNC_INVALID_SR 并计入 sr_rejected。五、漂移按 rtp 差除以 clock_rate 折算成毫秒再减 ntp 差，累计到最新 SR，跨 SR 用最新值覆盖而不是逐条相加；漂移绝对值超过 warn_ms 计一次 drift_warnings，同一 SR 只计一次。六、补偿比例等于 1 加漂移除以映射建立以来的 ntp 跨度，返回前夹在 0.98 与 1.02 之间，出界计入 ratio_clamped。七、每条流最多保留 max_samples 个 SR 样本，超出淘汰最旧并计入 samples_dropped。八、request_primary 只登记候选并返回 pending，候选流的 SR 连续两次确认后才完成切换，单次异常 SR 不算确认。九、enqueue_audio 水位是当前可播放音频的毫秒深度，不是队列里的包数；低于 low_watermark_ms 补静音到下限，高于 high_watermark_ms 丢最旧到上限，返回 action、padded_ms、dropped_ms、buffered_ms。十、schedule_video 用漂移比例换算校正时刻，早于当前超过 early_ms 返回 wait，晚于当前超过 late_ms 返回 drop，其余 render。十一、snapshot 深拷贝 primary、pending 与 streams，恢复后首个 SR 只刷新映射，不产生假偏差尖峰；restore 结构非法抛 SYNC_SNAPSHOT_INVALID 且原状态不变。十二、stats 固定返回 streams_registered、streams_removed、sr_ingested、sr_rejected、samples_dropped、ratio_clamped、drift_warnings、audio_padded_ms、audio_dropped_ms 九个计数。

硬约束：ingest_sr、request_primary、enqueue_audio、schedule_video 共用一把可重入锁，三路并发各上报 20 条 SR 后 sr_ingested 必须正好等于 60；每流样本按条数封顶，禁止无界增长；音频补偿累计量不得为负；统计口径按流独立，不随快照回滚。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发与重启恢复场景自行验证；只做本地实现，不涉及上游提交。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- name: batch48-py-game-c-02
- domain: 游戏、图形与多媒体
- sub: 音视频处理
- type: Bugfix
- difficulty: 困难
- module: av_drift_sync_guard
- typeName: AvDriftSyncGuard
