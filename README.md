# batch48-py-game-c-13

## 题面

带 B 帧的片源送进解码器，输出顺序整段错乱，片尾最后几帧总是丢；直播流偶尔不带 DTS，时间戳一进缓冲就来回跳，播放器追帧追到花屏。另一路点播里，迟到帧越积越多，进程内存一路涨到 3 GB 才被运维杀掉；EOS 到达时缓冲还剩十几帧，冲刷逻辑按到达顺序吐出，画面直接倒放。还有一种片源，B 帧的 DTS 与 PTS 顺序相反，缓冲里排好的次序一出队就乱；线上只能靠重启播放器临时缓解，重启后缓冲清空，首帧又要再等一轮。时间戳中间断了几秒的片源也被当成重排，缓冲白等一轮，首帧晚了半秒才出。

范围是 gsb/batch48-py-game-c-13 下的 pts_reorder_guard.py，类 PtsReorderGuard，负责解码侧按 DTS 入堆、按 PTS 出帧、最大重排深度控制、缺口统计与 EOS 冲刷。

对外接口：`PtsReorderGuard(*, max_reorder_depth: int = 16, gap_threshold_ms: int = 500, clock=None)`；`push(pts: int, dts: int | None = None, *, payload_size: int = 0, keyframe: bool = False) -> dict`；`pop(*, count: int = 1) -> list[dict]`；`flush_eos() -> list[dict]`；`buffered() -> int`；`last_output_pts() -> int | None`；`last_output_dts() -> int | None`；`late_dropped() -> int`；`gap_count() -> int`；`snapshot() -> dict`；`restore(state) -> None`；`stats() -> dict`；`reset_stats() -> None`。拒绝统一抛 ReorderError(code, detail)。

故障与边界：一、push 成功返回 accepted 为真、dropped 为假、buffered 为当前深度、released 为空、reason 为空，缓冲按 PTS 升序维护；二、pop 按 PTS 从小到大吐出，返回每帧的 pts、dts、keyframe、payload_size，count 不足时只吐剩余的；三、PTS 必须是非负整数，否则抛 REORDER_PTS_INVALID；DTS 给出时必须是整数，否则抛 REORDER_DTS_INVALID；四、缓冲内重复 PTS 抛 REORDER_DUPLICATE_PTS 且不改变缓冲；五、超过最大重排深度的迟到帧丢弃并计数，late_dropped 累加，不无限缓存；六、EOS 冲刷按 DTS 升序输出剩余帧，结尾不丢帧，冲刷结果里不得夹带已输出过的帧，冲刷后缓冲清零；七、无 DTS 的流用 PTS 推断，输出时间戳保持单调；八、相邻输出 PTS 间隔超过 gap_threshold_ms 记一次缺口，按连续处理，缺口只计数不补齐，后续帧照常出队，不当作重排；九、输出顺序由 PTS 决定，last_output_pts 与 last_output_dts 跟随最近一次 pop；十、snapshot 只保存帧的 pts、dts、keyframe、payload_size 等元数据，不含帧数据本体，元数据顺序与缓冲内一致，恢复后还能继续按 PTS 出帧，restore 结构非法抛 REORDER_SNAPSHOT_INVALID 且原状态不变；十一、冲刷之后再 push 抛 REORDER_AFTER_EOS，不允许半途混入新帧；十二、stats 固定返回 pushed、popped、released、late_dropped、flushed 五个整数，reset_stats 全部归零。

硬约束：push、pop、flush_eos、snapshot、restore 全程持锁，八路并发各推一帧时 buffered 必须等于成功接收的帧数，pushed 计数不得丢失或重复；同一缓冲的推入与取出串行，不得出现同一帧既在缓冲又被吐出；快照恢复后不与恢复前共享可变结构；入队与出队不得为了凑顺序整表重排，缓冲规模受 max_reorder_depth 约束，长片源上单帧操作保持常数级开销，禁止无界增长。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、不得跳过、不得按固定输入写死；EOS 冲刷与超深迟到自行验证；只做本地实现，不涉及上游提交。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 音视频处理
- type: Bugfix
- difficulty: 地狱
- module: pts_reorder_guard
- typeName: PtsReorderGuard
