# batch48-py-game-c-08

## 题面

播放器在第二个分片就 seek 错位，抓包发现 trun 的 data_offset 只按第一个 trun 算；断点续封重启之后 tfdt 还倒退了一截，跳转到分片边界时画面卡成黑帧，运维只能整段回源重拉。封装侧最早只处理单轨单片，后来加了音视频交错与续封，偏移、时间基准和切片时机各算各的，录播回看只要跨片跳转就对不上。范围是 `gsb/batch48-py-game-c-08` 下的 `fmp4_mux_guard.py`，类 `Fmp4MuxGuard`，负责样本排队、moof 与 mdat 的偏移记账、tfdt 推进和分片落盘。

公开接口：`Fmp4MuxGuard(*, stream_id='default', max_fragment_bytes=524288, timescale=1000)`；`set_track(track, *, timescale=None) -> dict`；`tracks() -> dict`；`add_sample(track, *, dts, duration, size, keyframe=False) -> dict`；`pending_bytes() -> int`；`should_flush() -> bool`；`flush() -> dict`；`fragments() -> list`；`sequence() -> int`；`sample_offsets(fragment_seq) -> list`；`tfdt_for(track) -> int`；`snapshot() -> dict`；`restore(state) -> None`；`stats() -> dict`；`reset_stats() -> None`。拒绝统一抛 `Fmp4Error(code, detail)`，code 取 FMP4_INVALID_SAMPLE、FMP4_UNKNOWN_TRACK、FMP4_TIMESCALE_INVALID、FMP4_EMPTY_FRAGMENT、FMP4_FRAGMENT_NOT_FOUND、FMP4_SNAPSHOT_INVALID。

故障与边界：一、mfhd 序列号从 1 起，每片加一，只增不减，flush 失败不占号也不复位。二、trun 的 data_offset 相对 moof 起点，多个 trun 的偏移接在上一段之后继续累计，不是各自从零算。三、tfdt 的 baseMediaDecodeTime 等于该轨在此片之前已封装时长之和，逐条按样本自带的 duration 累加，按各轨自己的 timescale 折算，不得回退。四、重启后首片 tfdt 从快照续起，既不回退也不跳空。五、关键帧写 SYNC_SAMPLE_FLAGS 0x02000000，其余写 DELTA_SAMPLE_FLAGS 0x01010000。六、音视频样本按到达顺序交错，连续同轨样本共用一个 trun，换轨即开新 trun，run_index 从零递增。七、单片大小超过 max_fragment_bytes 时只在下一个关键帧处切，不切在 P 帧中间；切分不得丢样本，分片里的样本总数必须与出队条数一致。八、单样本本身超过上限、dts 为负、时长或大小非正、同轨 dts 回退，一律报 FMP4_INVALID_SAMPLE；add_sample 返回 track、dts、duration、size、keyframe、index 六个字段。九、未登记的轨报 FMP4_UNKNOWN_TRACK，timescale 非正报 FMP4_TIMESCALE_INVALID，轨名为空同样按非法样本拒绝。十、空队列 flush 报 FMP4_EMPTY_FRAGMENT 且不消耗序列号，查询不存在的分片报 FMP4_FRAGMENT_NOT_FOUND。十一、mdat_size 等于样本大小之和加 8，byte_length 等于 moof_size 加 mdat_size；每条样本记录带 track、index、data_offset、size、sample_flags、duration，fragments 返回历史分片的深拷贝。十二、snapshot 深拷贝队列、序列号、各轨 tfdt 与历史，restore 校验 version 与结构，非法报 FMP4_SNAPSHOT_INVALID 且原状态不变。

硬约束：add_sample 与 flush 会被采集线程和封装线程并发调用，必须共用一把锁串行，四轨并发各加二十五条后计数恰好一百，队列里不允许出现被覆盖或丢半条样本；flush 要么整片成功要么队列完全不动。should_flush 只在队列总量达到上限且最后一个入队样本是关键帧时返回真，返回假时调用方继续排队。快照在锁内取，恢复后序列号与各轨 tfdt 必须自洽。单片大小上限是硬顶，任何情况下不得产出超过上限的分片，单样本超限直接拒收而不是切碎。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；多 trun 偏移、断点续封与关键帧切分自行验证；只做本地实现，不写真实文件、不依赖 ffmpeg 或任何封装库。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- name: batch48-py-game-c-08
- domain: 游戏、图形与多媒体
- sub: 音视频处理
- type: Bugfix
- difficulty: 困难
- module: fmp4_mux_guard
- typeName: Fmp4MuxGuard
