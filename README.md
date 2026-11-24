# batch48-py-game-c-15

## 题面

直播切广告之后正片少播了十五秒，回看时广告结束点比节目单早了整整一段；重连的观众又把同一支广告完整看了一遍。有一次插入点落在两个关键帧中间，解码器从半个画面开始解，先花后正常；还有一次广告时长比节目剩余时间还长，播控把后面的节目整体顺延，节目单全乱了。播控值班说那支广告的插入命令只带了相对时长，节目单上又没标结束点，截断和顺延两边各算一套，事后对不上账。运营在控制台点了取消，广告还是照播；同一批插入指令重放一遍，已播过的广告又触发一次，计数直接翻倍。

范围是 gsb/batch48-py-game-c-15 下的 scte35_splice_guard.py，类 Scte35SpliceGuard，负责插入点解析、待执行队列、关键帧执行、断点去重与取消。

对外接口：`Scte35SpliceGuard(*, max_pending: int = 256, clock=None)`；`parse_command(command: dict, *, program_end_ms: int | None = None) -> dict`；`schedule(command: dict, *, program_end_ms: int | None = None) -> dict`；`on_keyframe(keyframe_pts_ms: int) -> list[dict]`；`cancel(splice_id: str) -> bool`；`pending() -> list[dict]`；`triggered_ids() -> list[str]`；`executions() -> list[dict]`；`is_triggered(splice_id: str) -> bool`；`snapshot() -> dict`；`restore(state) -> None`；`stats() -> dict`；`reset_stats() -> None`。拒绝统一抛 Scte35SpliceError(code, detail)。

故障与边界：一、命令必须带非空 splice_id 与非负 splice_pts_ms，break_duration_ms 缺省为零，字段缺失或类型不对抛 SPLICE_INVALID；二、解析结果固定为 splice_id、splice_pts_ms、requested_duration_ms、effective_duration_ms、end_pts_ms 五个字段，解析不改动调用方传入的命令；三、插入点晚于或等于节目结束点抛 SPLICE_BEYOND_PROGRAM；四、break_duration 超过节目剩余时长时截断到剩余长度，end_pts 不超过节目结束点，而不是顺延后续节目；五、待执行队列按 splice_pts_ms 升序排列，同一 splice_id 重复排队抛 SPLICE_DUPLICATE；六、待执行数量达到 max_pending 时再排抛 SPLICE_QUEUE_FULL 且不落账；七、cancel 只对仍在排队的 splice_id 生效，返回真并移除，未知或已触发的返回假；八、插入点后移到其后的最近关键帧，偏移量写入执行记录，执行记录带 splice_id、splice_pts_ms、triggered_pts_ms、offset_ms、effective_duration_ms、end_pts_ms，记录按发生顺序追加、不覆盖旧记录；九、关键帧早于插入点时什么都不执行，待执行队列保持不变；十、同一 splice_id 只触发一次，断点恢复后不重复触发；十一、同一关键帧命中的多个插入点按 splice_pts_ms 升序依次执行，同一关键帧只结算一次；十二、snapshot 深拷贝待执行队列、已触发集合、执行记录与统计，restore 结构非法抛 SPLICE_SNAPSHOT_INVALID 且原状态不变。

硬约束：parse_command、schedule、on_keyframe、cancel 全程持锁，八路并发各排一条互不相同的插入指令时，一次关键帧必须让八条各执行一次，executions 长度与 triggered 计数都要等于八；解析与触发串行，不得出现同一条指令被执行两次或漏执行；解析结果不得直接引用调用方传入的字典，排队之后改动原对象不影响已排队内容；快照恢复后不得与恢复前共享可变对象；待执行队列受 max_pending 约束，禁止无界增长。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、不得跳过、不得按固定输入写死；时长截断与断点去重自行验证；只做本地实现，不涉及上游提交。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 音视频处理
- type: Bugfix
- difficulty: 地狱
- module: scte35_splice_guard
- typeName: Scte35SpliceGuard
