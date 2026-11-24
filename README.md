# batch48-py-game-b-23

## 题面

跑酷关卡接上事件轨道后，落地音效按帧号触发而不是按毫秒，低帧率机器上整段音效挤成一团；循环片段的区间查询在接缝处把同一个事件报两次，按循环序号去重也没做；根运动提取把手上骨骼的位移连同骨骼缩放一起算了进去，角色一放大就漂移；剪辑师每次换片段还要手工重挂事件表，快照恢复后事件计数又对不上。范围是 gsb/batch48-py-game-b-23 下的 animation_event_track.py，类 AnimationEventTrack，负责片段登记、毫秒采样、事件排序与区间查询、事件增删、根运动提取与快照。

公开接口：AnimationEventTrack()；load_clip(clip) -> dict；clip_info(clip) -> dict；list_clips() -> list[str]；add_event(clip, event) -> str；events(clip) -> list[dict]；events_between(clip, from_ms, to_ms) -> list[dict]；remove_event(clip, event_id) -> bool；clear_events(clip) -> int；event_count(clip) -> int；sample(clip, time_ms) -> dict；extract_root_motion(clip, from_ms, to_ms) -> dict；snapshot() -> dict；restore(state) -> None。所有拒绝抛 AnimationError(code)，code 取 ANIM_CLIP_INVALID、ANIM_CLIP_DUPLICATE、ANIM_UNKNOWN_CLIP、ANIM_EVENT_INVALID、ANIM_EVENT_OUT_OF_RANGE、ANIM_EVENT_LIMIT、ANIM_SNAPSHOT_INVALID。

故障与边界：一、采样一律按毫秒，骨骼位置与缩放都做线性插值，不得按帧号或像素取整，sample 返回 time_ms、loop_index 与按骨骼名索引的 bones，clip_info 返回 name、duration_ms、loop、bone_count 与 event_count；二、非循环片段超出时长按首尾值钳制并返回钳制后的有效时刻，循环片段按 duration_ms 取模并给出循环序号，负时间按零处理；三、事件按时间升序、同时间按优先级降序、再按事件 id 升序稳定排序；四、events_between 左闭右开，from 恰好等于事件时刻要收，to 恰好等于事件时刻要丢，区间结果每项含 event_id、name、time_ms、loop_index、priority；五、循环片段的事件按循环展开成绝对时刻，按循环序号区分，片段结束事件与开始事件落在同一时刻时只能报一次；六、add_event 校验名称非空、时刻落在 0 到时长之间，事件 id 由片段名与递增序号组成，单片段事件上限 256，超限整份拒绝；七、remove_event 对不存在的 id 幂等返回假，clear_events 返回实际删除条数；八、根运动只取名为 root 的骨骼平移，其他骨骼的位移不计，骨骼缩放带来的位移必须剔除，只保留平移分量；九、根运动区间端点落在关键帧之间时按插值求差，不按整帧取整；十、非法片段结构、重复片段名、时长非正、关键帧时间非严格递增都整份拒绝且不落地，对未登记片段做采样、查询与事件增删一律报 ANIM_UNKNOWN_CLIP；十一、快照含 version 与 clips，必须是深拷贝，恢复后片段、事件与采样结果逐项一致，结构非法整份拒绝并保持原状态；十二、并发采样与添加事件不得看到半更新的事件表。

硬约束：多路采样与事件增删同时进行时，采样只能看到增删前或增删后的完整事件表，事件计数与排序不得错位；进程重启后凭快照恢复，片段与事件逐项一致；单片段事件数有硬顶，达到上限后新增一律拒绝而不是覆盖旧事件。

验收：python3 -m pytest -q tests 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；循环接缝与并发采样场景自行验证；只做本地实现，并附一段改动说明，写清循环序号与区间边界的判定口径。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 图形渲染
- type: Bugfix
- difficulty: 困难
- module: animation_event_track
- typeName: AnimationEventTrack
