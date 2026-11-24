# batch48-py-game-c-04

## 题面

一个房间三十个观众同时进房，编码器被关键帧请求打爆，关键帧反倒更慢，观众黑屏时间从一秒拖到五秒；有人中途离开，请求还挂在队列里，下一个关键帧到了才清掉；弱网时同一路流每隔两百毫秒就被不同观众各要一次关键帧，编码器刚出完一帧又得重编。排查发现请求没有任何合并，每个订阅者的预算窗口按分钟算且离开不回收，请求发出去没有回执也没有重试，流还没发布时请求直接报错丢掉；这套关键帧请求最早是给一对一通话写的，进了多人房间以后同一个编码器要同时服务几十个订阅者，请求仍然按人到人直发，没有任何房间级视图。范围是 `gsb/batch48-py-game-c-04` 下的 `keyframe_request_guard.py`，类 `KeyframeRequestGuard`，负责请求合并、订阅者预算、退避重试与流发布状态。

公开接口：`KeyframeRequestGuard(*, min_interval_ms=500.0, budget=3, budget_window_ms=10000.0, retry_delay_ms=1000.0, max_retries=2, max_subscribers=64, clock=None)`；`add_subscriber(stream_id, subscriber_id) -> dict`；`remove_subscriber(stream_id, subscriber_id) -> dict`；`request_keyframe(stream_id, subscriber_id, *, now_ms) -> dict`；`on_keyframe(stream_id, *, now_ms) -> dict`；`publish_stream(stream_id) -> dict`；`stream_info(stream_id) -> dict`；`pending(stream_id) -> dict | None`；`next_retry_ms(stream_id) -> float | None`；`due_retries(now_ms) -> list`；`budget_remaining(stream_id, subscriber_id, *, now_ms) -> int`；`snapshot() -> dict`；`restore(state) -> None`；`stats() -> dict`；`reset_stats() -> None`。拒绝统一抛 `KeyframeRequestError(code, detail)`，code 取 KF_UNKNOWN_STREAM、KF_UNKNOWN_SUBSCRIBER、KF_DUPLICATE_SUBSCRIBER、KF_SUBSCRIBER_LIMIT、KF_SNAPSHOT_INVALID。

故障与边界：一、add_subscriber 为流自动建档，同一流重复订阅同一人报 KF_DUPLICATE_SUBSCRIBER，单流订阅者超过 max_subscribers 报 KF_SUBSCRIBER_LIMIT。二、request_keyframe 的流或订阅者未登记分别报 KF_UNKNOWN_STREAM 与 KF_UNKNOWN_SUBSCRIBER。三、流未发布时请求只登记为延迟请求，发布时补发一次，不重复发送。四、同一流在最小间隔内的再次请求合并到现有请求，返回 coalesced 与原请求编号，不消耗预算。五、每个订阅者按 budget_window_ms 滚动窗口持有 budget 次预算，窗口从首次使用起算，用尽后拒绝返回 denied 并计入 denied。六、距离上次发出不足 min_interval_ms 的请求返回 throttled，不消耗预算。七、请求发出后 1 秒没有关键帧则按 retry_delay_ms 的两倍指数退避重试，最多重试两次，用尽计入 retries_exhausted。八、关键帧到达清除 pending 与退避计划，返回 cleared 与等待人数。九、订阅者离开时清理其 pending 与已用预算，不影响其他订阅者。十、due_retries 只返回到期的重试，next_retry_ms 给出下一次到期时刻，耗尽返回 None。十一、snapshot 深拷贝 pending、预算窗口与请求编号，restore 非法报 KF_SNAPSHOT_INVALID 且原状态不变。十二、stats 固定返回 subscribers_added、subscribers_removed、streams_published、requests_sent、coalesced、throttled、denied、retries、retries_exhausted、keyframes 十个整数。

硬约束：request_keyframe、on_keyframe、due_retries 共用一把可重入锁，四路并发各自订阅独立流各发四次时 requests_sent 正好 16 且请求编号不重复；每流 pending 至多一个，禁止同一流堆叠多个 pending；每次合并与重试都要有请求编号可追踪，编号单调递增且不重用；预算窗口按订阅者独立，不得互相挤占。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发与重启恢复场景自行验证；只做本地实现，不涉及上游提交。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- name: batch48-py-game-c-04
- domain: 游戏、图形与多媒体
- sub: 音视频处理
- type: Bugfix
- difficulty: 困难
- module: keyframe_request_guard
- typeName: KeyframeRequestGuard
