# batch48-py-game-c-11

## 题面

监控大屏上，一路连续两小时的直播在包序号跨过 65535 之后，接收报表里的累计丢包从两位数跳成四百多万，隔天再看还是那个数字；运维把接收统计进程重新拉起，分片丢失率又在半分钟里死死停在零。弱网时段更难受，抖动值每来一个包就上下乱跳，接收端的缓冲深度跟着一起抖；另外几路流的报告块里，LSR 与 DLSR 填的竟是本机时间，发送端据此算出的往返时延偏出几百毫秒。上周还有一路汇聚流同时带四十个 SSRC，报告块一次性全塞进去，报文超长被对端整包丢弃。

范围是 gsb/batch48-py-game-c-11 下的 rtcp_feedback_guard.py，类 RtcpFeedbackGuard，负责接收侧序号展开、丢包与抖动统计，以及报告块生成。

对外接口：`RtcpFeedbackGuard(*, max_report_blocks: int = 31, max_streams: int = 32, jitter_scale: int = 16, clock=None)`；`on_packet(ssrc: int, seq: int, *, arrival_ms=None, payload_size: int = 1000) -> dict`；`on_sender_report(ssrc, *, ntp_seconds, ntp_fraction, arrival_ms=None) -> dict`；`highest_seq(ssrc) -> int`；`cumulative_lost(ssrc) -> int`；`fraction_lost(ssrc) -> int`；`interarrival_jitter(ssrc) -> int`；`report_block(ssrc, *, now_ms=None) -> dict`；`build_report(*, reporter_ssrc=0) -> dict`；`restart() -> None`；`ssrcs() -> list[int]`；`snapshot() -> dict`；`restore(state) -> None`；`stats() -> dict`；`reset_stats() -> None`。拒绝统一抛 RtcpFeedbackError(code, detail)。

故障与边界：一、序号按十六位回绕展开成连续扩展序号，跨过 65535 后最高序号继续增大；二、期望包数按扩展最高序号减起始序号加一计算，重复序号只算收过一次，累计丢包不随重复包变化；三、累计丢包按二十四位有符号字段输出，超出表示范围时按有符号回绕；四、fraction_lost 只用本区间数据，本区间期望为扩展最高序号减上次出报告时的序号，丢失乘 256 整除期望并封顶 255，出报告块后区间归零；五、抖动取相邻到达间隔差的绝对值，按十六分之一整数平滑，不足两个间隔时保持零；六、到达毫秒必须单调，回退抛 RTCP_ARRIVAL_INVALID，序号越界抛 RTCP_SEQ_INVALID；七、LSR 取最近一次发送报告 NTP 时间戳的中间三十二位，DLSR 为距该报告的毫秒数乘 65536 整除 1000，没有发送报告时两者都为 0；八、查询未登记的 SSRC 抛 RTCP_UNKNOWN_SSRC；九、同时在册的流超过 max_streams 抛 RTCP_TOO_MANY_STREAMS 且不落账；十、build_report 最多输出 max_report_blocks 个报告块，按最近活动挑选、按 SSRC 升序排列，超出数量放进 omitted；十一、restart 之后区间统计从零起算，累计丢包与最高序号保留，发送报告记录清空；十二、snapshot 深拷贝全部流状态与统计，restore 结构非法抛 RTCP_SNAPSHOT_INVALID 且原状态不变。

硬约束：on_packet、on_sender_report、report_block、build_report 全程持锁，八路并发各推一路流时 packets 总数必须等于调用次数，不能丢计数也不能重复计数；同一 SSRC 的收包与出报告串行，报告块不得出现半更新状态；快照恢复后不得与恢复前的结构共享可变对象；在册流数量受 max_streams 约束，禁止无界增长。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、不得跳过、不得按固定输入写死；并发与重启后的统计变化自行验证；只做本地实现，不涉及上游提交。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 音视频处理
- type: Bugfix
- difficulty: 地狱
- module: rtcp_feedback_guard
- typeName: RtcpFeedbackGuard
