# batch48-py-safety-a-23

## 题面

隧道网关上线大消息中转后，客服和值班同时收到两类反馈：一条几十兆的配置推送收齐了所有分片仍然拼不出来，另一条早就超时的消息隔了几分钟突然被投递，内容里一半是上一条连接留下的旧片；并发高峰时一台网关被同一条流上的上百个半成品消息拖住，新消息全部被拒，重启之后未完成的消息又从零开始计时，对端只要每隔几秒补一片就能永久占着缓冲区；运维想看某条消息还缺哪几片，接口却只给出一个总数。范围是 gsb/batch48-py-safety-a-23 下的 message_fragment_guard.py，类 MessageFragmentGuard，按 msg_id 与 epoch 维护片位图、字节计数和每流并发重组上限。

公开接口：MessageFragmentGuard(*, max_inflight_per_stream: int = 4, max_bytes_per_stream: int = 65536, max_fragments_per_stream: int = 64, reassembly_timeout: float = 5.0, clock=None)；on_fragment(stream: str, msg_id: int, epoch: int, index: int, count: int, total_len: int, data: bytes, now=None) -> bytes | None；pending_messages(stream: str) -> list；stream_state(stream: str) -> dict；expire(now) -> list；close_stream(stream: str) -> int；drop_message(stream: str, msg_id: int, epoch: int) -> bool；snapshot() -> dict；restore(state: dict) -> None；stats() -> dict。拒绝统一抛 FragmentError(code)，code 取 FRAG_INVALID_FRAGMENT、FRAG_INDEX_OUT_OF_RANGE、FRAG_INCONSISTENT、FRAG_LENGTH_MISMATCH、FRAG_STREAM_LIMIT、FRAG_SNAPSHOT_INVALID。

要修的点：一、重复片直接忽略并计入 duplicate_ignored，不得改变字节计数；二、片索引越界，或者 count、total_len 与已登记消息不一致，整条拒绝并释放已占预算、计入 rejected；三、只有 0 到 count-1 全部到齐才按 index 升序拼接，拼出的总长度必须等于 total_len，不等整条拒绝；四、缺片超时由 expire 整条丢弃并逐条上报 msg_id、epoch 与缺失片序号，不交付半条；五、每流的半成品消息数、声明总字节、分片数都是硬上限，超限拒收新消息，不得挤掉已有消息腾地方；六、消息交付、丢弃或关闭流之后立即释放其字节与片数预算；七、stream 为空、msg_id 或 epoch 为负、count 小于 1、index 为负或超出 count、total_len 为负、data 不是字节串都报非法；八、快照恢复后未完成消息继续拼装、计数保留，结构非法报 FRAG_SNAPSHOT_INVALID 且不动原状态；九、快照里未完成消息的首次到达时刻必须保留，恢复后缺片超时从首次到达时刻继续计算，不能重新计时让对端无限延长占用；十、同一 msg_id 在超时丢弃后又被重用为另一条消息时必须能区分，不能把旧片拼进新消息；十一、最后一片到达时字节数已经等于 total_len 但仍有片缺失，必须判缺片继续等待，不能按长度提前交付；十二、stats 固定返回 delivered、timeout_dropped、closed_dropped、duplicate_ignored、rejected 五个整数，pending_messages 给出 msg_id、epoch、count、total_len、have、received_bytes，stream_state 给出 inflight、received_bytes、reserved_bytes、fragments。

硬约束：on_fragment、expire、close_stream、drop_message 全程持同一把逻辑锁，同一消息同一片在并发下只记一次，整条消息只投递一次，统计与调用次数严格相等；每流占用按声明值预留，恢复后预算不得凭空放大；快照跨进程恢复后不共享可变结构。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发重组与重启续拼自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 网络与通信安全
- type: Bugfix
- difficulty: 困难
- module: message_fragment_guard
- typeName: MessageFragmentGuard
