# batch48-py-game-c-09

## 题面

换台时新 PMT 还没收全，旧表就被清掉，画面黑了半秒；带 pointer_field 的段落总被当成坏包丢弃，跨包拼出来的段还带着上一包的填充字节，长度一算就超。早期解封装只处理单包整段，后来接上多节目流，版本替换、CRC 校验与 PES 拼接各自写在不同的回调里，换台、切音轨和插播轮播都会踩到。范围是 `gsb/batch48-py-game-c-09` 下的 `psi_section_guard.py`，类 `PsiSectionGuard`，负责 PSI 段重组、版本替换、加扰拒绝与 PES 跨包拼接。

公开接口：`PsiSectionGuard(*, max_section_bytes=4096, max_pes_bytes=1048576)`；`feed_section(pid, section, *, scrambling=0) -> dict`；`feed_payload(pid, payload, *, unit_start=False, scrambling=0) -> dict`；`tables() -> dict`；`version_of(table, *, pid=None) -> int`；`pending_bytes(pid) -> int`；`discard(pid) -> int`；`push_pes(stream_id, chunk, *, pts) -> dict`；`flush_pes() -> list`；`drain_pes() -> list`；`snapshot() -> dict`；`restore(state) -> None`；`stats() -> dict`；`reset_stats() -> None`。拒绝统一抛 `PsiError(code, detail)`，code 取 PSI_MALFORMED_SECTION、PSI_CRC_MISMATCH、PSI_DUPLICATE_PID、PSI_BUFFER_OVERFLOW、PSI_STALE_VERSION、PSI_PES_OVERFLOW、PSI_SNAPSHOT_INVALID、PSI_UNKNOWN_TABLE。

段格式：首字节 table_id，0x00 为 PAT、0x02 为 PMT；随后两字节大端 section_length，值等于版本字节、body 与四字节 CRC 之和；再一字节版本，取值 0 到 31；body 之后是四字节 CRC-32/MPEG-2，多项式 0x04C11DB7，初值全 1，不反转也不做末尾异或，校验范围是 CRC 之前的全部字节。PAT 的 body 是若干四字节条目，节目号与 PID 各占两字节；PMT 的 body 前两字节是 PCR PID，其后是若干两字节的 elementary PID。tables 返回 pat 与 pmt 两部分，pmt 以 PMT PID 为键；feed_section 返回 table、version、pid 三个字段，feed_payload 返回本次解析出的段数与剩余缓冲字节数。

故障与边界：一、长度字段与实际长度不符、版本超过 31、PAT body 不是四的倍数，一律报 PSI_MALFORMED_SECTION，先判长度再算 CRC。二、CRC 不符报 PSI_CRC_MISMATCH，整段丢弃并计数，已有表不动。三、PMT 内重复 elementary PID 报 PSI_DUPLICATE_PID，整段拒绝。四、新版本表在收全之前继续使用旧表，不清空现有映射。五、收全后整表替换不合并，版本号更小的段报 PSI_STALE_VERSION，同版本重复到达按幂等覆盖处理。六、pointer_field 之后才开始 section，跨包拼接不把填充字节算进段长。七、段长超过 max_section_bytes 报 PSI_BUFFER_OVERFLOW 并清空该 PID 缓冲，调用方从下一个 unit_start 重新开始拼。八、加扰控制非零的段直接拒绝并计数。九、PES 按 stream_id 与 pts 归并跨包数据，同一键的多段首尾相接。十、flush_pes 把未完成的 PES 收口，drain_pes 按 PTS 升序输出并清空，单位记录带 stream_id、pts、length。十一、单条 PES 超过 max_pes_bytes 报 PSI_PES_OVERFLOW 并丢弃该条。十二、snapshot 深拷贝表、版本与缓冲，restore 校验 version 和字段类型，非法报 PSI_SNAPSHOT_INVALID 且原状态不变。

硬约束：push 与表查询会被解复用线程和分析线程并发调用，必须共用一把锁串行，三路并发各推五段后计数恰好十五、三张表都在；读到的表要么全旧要么全新，不得半更新。缓冲有上限，超限丢弃并等待下一段，内存不随流时长增长；discard 清空指定 PID 的待拼缓冲并返回清掉的字节数。查未安装的表或未登记的 PMT PID 报 PSI_UNKNOWN_TABLE。stats 固定返回 sections_parsed、crc_errors、rejected、pes_units、overflows、scrambled 六个整数，同版本幂等覆盖也计入 sections_parsed。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；版本替换与 pointer_field 自行验证；缓冲与 PES 上限都按字节硬顶，只做本地实现，不接真实码流，也不依赖第三方解析库。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- name: batch48-py-game-c-09
- domain: 游戏、图形与多媒体
- sub: 音视频处理
- type: Bugfix
- difficulty: 困难
- module: psi_section_guard
- typeName: PsiSectionGuard
