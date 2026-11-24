# batch48-py-game-c-12

## 题面

晚间场观众切换清晰度，从高清切到标清时画面冻了两秒，再切回高清直接绿屏；复盘发现切换指令一到就改了转发目标，目标层的关键帧根本没等。另一路更糟：目标层压根没发布，切换接口却把当前层清空，观众黑屏直到重连；还有几次切换请求挂住不回退，旧层也不再有帧送出去。运维在边缘节点抓包，切换窗口里只剩下新层的零星几帧，旧层早就停了；切回高清那次更明显，画面从中间一条线开始花，十几秒才恢复。同一个观众反复点同一档位，每次都被当成新请求，日志刷出几十条告警；暂停后恢复播放，第一帧是半张脸，得等下一次关键帧才正常。

范围是 gsb/batch48-py-game-c-12 下的 simulcast_layer_guard.py，类 SimulcastLayerGuard，负责大小流层的发布状态、订阅切换、暂停恢复与质量采样。

对外接口：`SimulcastLayerGuard(*, layers=("low", "mid", "high"), initial="low", switch_timeout_ms=2000, upgrade_confirm_samples=3, quality_thresholds=(40, 75), clock=None)`；`publish_layer(layer, *, now_ms=None) -> dict`；`unpublish_layer(layer, *, now_ms=None) -> dict`；`switch(target, *, now_ms=None) -> dict`；`on_frame(layer, *, keyframe=False, now_ms=None) -> dict`；`active_layer() -> str | None`；`pending(*, now_ms=None) -> dict | None`；`pause(*, now_ms=None) -> dict`；`resume(*, now_ms=None) -> dict`；`report_quality(score, *, now_ms=None) -> dict`；`layer_order() -> list[str]`；`snapshot() -> dict`；`restore(state) -> None`；`stats() -> dict`；`reset_stats() -> None`。拒绝统一抛 SimulcastError(code, detail)。

故障与边界：一、publish_layer 返回 layer、published、active 三个字段，首个发布且等于 initial 的层成为活动层，重复发布同一层幂等；二、未知层名在发布与收帧两条路径上都抛 SIMULCAST_UNKNOWN_LAYER；三、未发布层收到帧抛 SIMULCAST_NOT_PUBLISHED；四、switch 到当前层直接返回 status 为 active，不产生待切换状态；五、switch 到已发布的其他层返回 status 为 pending，active 暂时不变，pending 记录目标、来源与请求毫秒；六、切层等待期间旧层持续转发，目标层只有关键帧才完成切换并回 switched 为真；七、目标层非关键帧不转发，原因记为 waiting_keyframe；八、目标层未发布时返回 status 为 unavailable，保持旧层并标记不可用；九、等待超过 switch_timeout_ms 放弃并回退旧层，pending 清空且计入 fallbacks；十、pause 后活动层帧不转发，resume 返回 requires_keyframe，恢复后首个关键帧才继续转发；十一、report_quality 按 quality_thresholds 取期望层，分数落在当前层区间时 direction 为 hold 且不发起切换；降层立即请求，升层需连续 upgrade_confirm_samples 个相同采样确认，换目标时确认计数清零；十二、snapshot 深拷贝发布表、暂停状态、待切换记录与统计，restore 结构非法抛 SIMULCAST_SNAPSHOT_INVALID 且原状态不变。

硬约束：publish_layer、unpublish_layer、switch、on_frame、report_quality 全程持同一把锁，八路并发送帧时 frames_forwarded 必须与转发返回为真的次数完全相等，不得两层帧交错；pending 的建立与完成必须原子，不能出现目标已切换但待切换记录还在的中间态；快照恢复后不得与恢复前共享可变对象；待切换记录最多一条，重复请求不新增；质量采样按秒上报，分数在档位边界抖动时不得来回升降，避免一小时里反复切换。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、不得跳过、不得按固定输入写死；切层期间旧层连续与超时回退自行验证；只做本地实现，不涉及上游提交。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 音视频处理
- type: Bugfix
- difficulty: 地狱
- module: simulcast_layer_guard
- typeName: SimulcastLayerGuard
