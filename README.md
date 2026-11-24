# batch48-py-game-a-16

## 题面

大世界拍卖行上线后事件通道开始丢账：玩家卖出一件紫装，客户端背包已扣，服务端流水里却没有这笔掉落；十几个人同时在王城放技能，飘字和特效卡了半秒，等带宽缓过来，必须可靠的掉落事件反而整批没发出去；同一次任务进度上报在弱网下被客户端重发，服务端把奖励结算了两遍；断线重连后补发的重传包把已经确认过的击杀事件又投递一次，对账脚本每天要人工抹掉重复流水；活动高峰时重传队列越挤越长，最后整条通道安静下来，没有任何告警，对账缺口到第二天才发现。范围是 gsb/batch48-py-game-a-16 的 reliable_event_guard.py，类 ReliableEventGuard，负责按事件类别分级投递、累积确认、TTL 过期记账与重传配额。

公开接口：ReliableEventGuard(*, max_retransmits=3, backoff_ticks=2, bandwidth_quota=8, dead_letter_limit=64)；register_channel(channel, *, reliable, mergeable=False) -> dict；publish(channel, event_id, payload=None, *, tick=0, ttl_ticks=6) -> dict；ack(channel, upto_seq) -> dict；pending(channel) -> list；pending_count() -> int；collect(tick) -> dict；dead_letters(channel=None) -> list；watermark(channel) -> int；next_seq(channel) -> int；channels() -> dict；stats() -> dict；reset_stats() -> None；snapshot() -> dict；restore(state) -> None。拒绝统一抛 EventError(code)。

要修的点：一、通道注册时声明可靠性与可合并性，重复注册抛 EVENT_DUPLICATE_CHANNEL，未注册通道上的发布与确认抛 EVENT_UNKNOWN_CHANNEL；二、可靠事件按通道分配从 1 开始严格递增的序号，发布返回 channel、event_id、seq、status，status 为 queued；不可靠事件只直发一次，seq 为 None，status 为 fire_and_forget，其 pending 恒为空、next_seq 恒为 1、watermark 恒为 0；三、同一幂等键在同一通道只交付一次，可靠事件再次发布返回 status duplicate 与原序号，已经确认过的幂等键也不得重新入队；四、可合并通道上同一幂等键再次发布返回 status merged，沿用原序号、以最后一次取值覆盖并按最后发布时间重算 TTL，队列条数不增加；五、确认是累积的：ack 清掉该通道所有小于等于 upto_seq 的待确认事件，返回 channel、acked 升序序号、watermark，水位只前进不后退，重复或过小的确认返回空 acked 且水位不变，upto_seq 必须是非负整数；六、collect 返回 tick、resend、expired、dead、bandwidth_used，只处理可靠事件，候选按 next_retry_tick 升序再按序号升序排列，首次发布不算重传，重传后 next_retry_tick 推进 backoff_ticks；七、重传次数达到 max_retransmits 的事件进死信并计入 dead_letters，导出接口要能按通道列出死信，不能静默丢弃让对账出现缺口，这类事件不再出现在 resend 里，retransmits 也不再多算；八、TTL 过期的事件记入 expired 只报一次，之后任何 tick 都不再补发；九、每次 collect 的重传总量受 bandwidth_quota 硬顶，超出的候选原样留到下一次 collect，不消耗重传次数，也不改变原定重试时刻；十、不可靠事件不占用任何带宽配额；十一、next_seq 返回该通道下一个将被分配的序号，watermark 返回已确认的最高序号，channels 返回每个通道的可靠性与可合并性；十二、stats 固定返回 published、sent_once、merged、duplicates、acked、retransmits、expired、dead_letters、collects 九个整数，reset_stats 归零但不清队列、序号与水印；十三、snapshot 返回带 version=1 的深拷贝，restore 后队列、重试时刻、水位与统计一致，结构非法抛 EVENT_SNAPSHOT_INVALID 且不破坏当前状态。

硬约束：publish、ack、collect 与快照读写全程持同一把锁，八路并发发布后的序号两两不同且连续；快照跨进程恢复后不得与恢复前的对象共享可变结构；死信日志条数受 dead_letter_limit 限制，超出淘汰最旧，内存不随在线时长增长。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发发布与恢复场景自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 游戏开发
- type: Bugfix
- module: reliable_event_guard
- typeName: ReliableEventGuard
