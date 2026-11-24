# batch48-py-game-c-10

## 题面

回看列表里有一半切片起播黑屏，重复上传又把同一段时间存了两份，清理任务还删掉了没传完的文件，客服只能让主播重新推流，运营侧的存储账单也对不上。早期录制按固定时长硬切，切片头落在 P 帧上，播放器解不出第一帧；上传又拿时间戳当键，重试一次就多一个对象，索引和账单两边各说各话。范围是 `gsb/batch48-py-game-c-10` 下的 `recording_slice_guard.py`，类 `RecordingSliceGuard`，负责关键帧对齐切片、索引清单、上传幂等与保留期清理。

公开接口：`RecordingSliceGuard(*, stream_id='stream-0', target_seconds=10.0, max_slice_bytes=8388608, retention_seconds=3600, max_retries=3, backoff_ms=200)`；`append(pts, duration, size, *, keyframe=False) -> dict`；`close() -> dict | None`；`slices() -> list`；`manifest() -> dict`；`slice_digest(slice_id) -> str`；`idempotency_key(slice_id) -> str`；`mark_uploaded(slice_id, *, ok) -> dict`；`retry_delay_ms(slice_id) -> int`；`pending_upload() -> list`；`cleanup(now) -> list`；`snapshot() -> dict`；`restore(state) -> None`；`stats() -> dict`；`reset_stats() -> None`。拒绝统一抛 `RecordingError(code, detail)`，code 取 REC_INVALID_SAMPLE、REC_UNKNOWN_SLICE、REC_RETRY_EXHAUSTED、REC_SNAPSHOT_INVALID。

故障与边界：一、PTS 为负、时长为零、大小为零都报 REC_INVALID_SAMPLE；append 返回 slice_id、seq、bytes 与 samples 四个字段。二、切片必须从关键帧开始，没有开片时收到非关键帧直接拒收并计入 rejected。三、已有开片时再来关键帧，且距开片已满 target_seconds，先收口旧片再以该关键帧开新片；未满则继续追加。四、切片大小超过 max_slice_bytes 时等到下一个关键帧才切，切片起点始终是关键帧，不允许切在帧中间。五、close 把开片收口，结束时间取最后一个样本的 PTS 加时长，没有开片返回 None。六、索引清单记录序号、起止 PTS、字节数与内容摘要，摘要取十六位小写十六进制，同一片重复读取不变。七、上传幂等键由流 ID、切片序号与摘要组成，同一片多次取值一致，重试不得产生第二个对象。八、上传失败按 backoff_ms 翻倍退避，最多重试 max_retries 次，超过报 REC_RETRY_EXHAUSTED 并置为 failed；失败返回 attempts 与 retry_in_ms，成功把状态置为 uploaded。九、上传成功的片从 pending_upload 移除，未上传的按序号排在前面。十、保留期清理只删已上传切片，未完成上传的文件必须保留，删除按结束时间与 now 之差判定。十一、snapshot 深拷贝清单与未完成切片，restore 校验 version 与结构，非法报 REC_SNAPSHOT_INVALID 且原状态不变。十二、stats 固定返回 samples、slices_closed、uploads、retries、cleaned、rejected 六个整数。

硬约束：append 与 close 会被采集线程和收口线程并发调用，必须共用一把锁串行，四路并发各加二十条后样本数恰好八十一、字节数恰好八百一十；清单读取要么全旧要么全新，不得出现半条记录。切片序号从 1 起单调递增，不复用也不跳号，重启恢复后继续沿用原序号。内存按片段封顶，清单最多保留 4096 条，超出淘汰最旧的已上传记录，未上传的不参与淘汰。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；保留期不删未上传、超限切分与幂等重试自行验证；所有时间都以毫秒整数参与比较，只做本地实现，不写真实文件、不接对象存储。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- name: batch48-py-game-c-10
- domain: 游戏、图形与多媒体
- sub: 音视频处理
- type: Bugfix
- difficulty: 困难
- module: recording_slice_guard
- typeName: RecordingSliceGuard
