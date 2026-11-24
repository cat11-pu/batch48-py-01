# batch48-py-game-c-14

## 题面

剪辑师把同一轨的两段素材拖到一起，导出后前一段凭空消失；给片段加的转场把手比素材本身还长，渲染直接报错退出。另一条时间线铺了三层轨道，导出时叠放次序反了，字幕压在被遮住的那一层；渲染区间中部跨过一段空轨，成片时长比设定少了半分钟。渲染区间是运维按毫秒填的，导出后首帧黑一帧、尾帧被切掉半帧，时长和帧率也对不上。两位剪辑同时改同一条时间线，保存后版本号只加了一次，后改的片段把前一个覆盖掉；把片段裁短之后，原来设好的转场把手还按旧长度计算，首帧就跳。

范围是 gsb/batch48-py-game-c-14 下的 timeline_compositor.py，类 TimelineCompositor，负责多轨片段放置、转场把手收敛、渲染计划生成与版本推进。

对外接口：`TimelineCompositor(*, frame_rate: int = 25)`；`add_track(name: str, *, order: int) -> dict`；`remove_track(name: str) -> None`；`tracks() -> list[dict]`；`add_clip(track: str, *, start_frame: int, duration_frames: int, source_in_frame: int = 0, transition_in_frames: int = 0, transition_out_frames: int = 0, clip_id: str | None = None) -> dict`；`trim_clip(clip_id: str, *, duration_frames: int) -> dict`；`move_clip(clip_id: str, *, start_frame: int) -> dict`；`remove_clip(clip_id: str) -> None`；`effective_transition(clip_id: str, side: str) -> int`；`render_plan(*, start_frame: int, end_frame: int) -> dict`；`version() -> int`；`clips(track: str | None = None) -> list[dict]`；`snapshot() -> dict`；`restore(state) -> None`；`stats() -> dict`；`reset_stats() -> None`。拒绝统一抛 TimelineError(code, detail)。

故障与边界：一、轨道名唯一，重名抛 TL_TRACK_EXISTS；叠放序号唯一，冲突抛 TL_TRACK_ORDER_CONFLICT；tracks 按叠放序号升序返回；二、同一轨片段区间重叠直接拒绝，抛 TL_CLIP_OVERLAP，不自动裁剪也不覆盖，失败时版本不变；三、片段字段固定为 clip_id、track、start_frame、duration_frames、end_frame、source_in_frame、transition_in_frames、transition_out_frames，未给 clip_id 时按 clip-1、clip-2 顺序生成；四、起始帧与时长必须合法，时长为零或负抛 TL_CLIP_INVALID，轨道不存在抛 TL_TRACK_UNKNOWN；五、两个转场把手之和超过片段时长抛 TL_TRANSITION_OVERFLOW；六、转场把手在素材被裁短时收缩到可用长度，不越过片段边界，effective_transition 按 in 与 out 两侧分别返回；七、trim_clip 只允许缩短，缩短后 end_frame 跟着变，非缩短抛 TL_TRIM_INVALID；八、render_plan 的起止帧必须满足起始小于结束且非负，否则抛 TL_RANGE_INVALID；九、渲染区间覆盖空轨时输出黑场而不是跳过，片段未覆盖的部分同样补黑场，时长不得变短；十、渲染计划按轨道叠放顺序列出，每段带 kind、start_frame、end_frame、clip_id、source_in_frame，片段段带裁剪后的源入点；十一、每次成功增删改都推进版本，版本号从零起、每次成功变更加一，失败操作不动版本，render_plan 只读取当前版本不推进；十二、snapshot 深拷贝轨道、片段、版本与统计，restore 结构非法抛 TL_SNAPSHOT_INVALID 且原状态不变。

硬约束：add_track、remove_track、add_clip、trim_clip、move_clip、remove_clip、render_plan 全程持锁，八路并发各改一条轨道时版本号必须与成功变更次数完全相等，不能丢版本也不能重复推进；编辑与渲染计划读取串行，计划里不得出现只改了一半的片段；渲染计划只读当前状态，不持有片段的可变引用，生成期间被并发改动不得污染已生成的计划；快照恢复后不得与恢复前共享可变对象；单轨片段数受统计口径约束，禁止无界增长。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、不得跳过、不得按固定输入写死；把手收缩与空轨黑场自行验证；只做本地实现，不涉及上游提交。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 音视频处理
- type: Bugfix
- difficulty: 地狱
- module: timeline_compositor
- typeName: TimelineCompositor
