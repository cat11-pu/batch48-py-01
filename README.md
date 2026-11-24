# batch48-py-safety-a-22

## 题面

分支办公室的 VPN 上周开始反复断开：对端反复重传，隧道另一侧的安全关联却把整段流量当成重放丢掉；运维把窗口调小之后，之前收到过的序号又能通过一次，审计里出现同一序号被接受两遍；某条安全关联到了软时限，在途报文仍被收下并转进内网，而硬时限到期删除后，用同名 SPI 重建的关联直接沿用了旧的窗口位置，一段早该丢弃的老包被放了进来；出故障时重放统计只有总数，值班查不出是哪条关联、哪个序号、因为什么被拒。范围是 gsb/batch48-py-safety-a-22 下的 esp_replay_guard.py，类 EspReplayGuard，按安全关联维护 64 位扩展序列号的滑动窗口与位图。

公开接口：EspReplayGuard(*, default_window: int = 64, max_report_records: int = 256, clock=None)；add_sa(sa_id: str, *, spi: int, window_size: int = 64, soft_lifetime: float = 3600.0, hard_lifetime: float = 7200.0, max_bytes: int = 1 << 30, now=None) -> dict；remove_sa(sa_id: str) -> bool；accept(sa_id: str, seq: int, *, length: int = 0, now=None) -> bool；set_window_size(sa_id: str, window_size: int) -> None；sa_state(sa_id: str) -> dict；list_sas() -> list；replay_report(sa_id: str) -> list；snapshot() -> dict；restore(state: dict) -> None；stats() -> dict。拒绝统一抛 EspError(code)，code 取 ESP_INVALID_ARGUMENT、ESP_INVALID_SEQ、ESP_UNKNOWN_SA、ESP_DUPLICATE_SA、ESP_SA_EXPIRED、ESP_SA_BYTE_LIMIT、ESP_SEQ_EXHAUSTED、ESP_SNAPSHOT_INVALID。

要修的点：一、accept 在窗口内判重放，已见序号返回 False 并计一次重放，低于窗口左沿返回 False 并计超窗，两种拒绝不得混算；二、序号前进时位图按差值移动，跨 64 位序号高半段时不得丢已见标记，也不得出现负数下标；三、序号到 2^64-1 之后继续收包报 ESP_SEQ_EXHAUSTED，任何超出 64 位范围的序号报 ESP_INVALID_SEQ；四、软时限到期后仍在途的包按拒绝处理，硬时限到期或字节数超过 max_bytes 时删除该安全关联并报错；五、乱序容忍范围由 window_size 决定，窗口缩小不得让已见序号重新变成可接受，窗口调大后更老的序号可以重新进入判定范围；六、字节记账只统计被接受的包，重放包与超窗包不占字节额度；七、spi 必须在 32 位范围内，window_size 至少为 1，软硬时限与 max_bytes 必须为正，软时限不得晚于硬时限；八、同一 sa_id 重复登记报 ESP_DUPLICATE_SA，remove_sa 幂等并返回是否删掉了东西；九、sa_state 要给出 spi、window_size、highest_seq、accepted、replayed、too_old、bytes_used；十、重放拒绝要能按 SA 给出序号与原因，只给总数不算完成，拒绝记录按 max_report_records 封顶、超出淘汰最旧；十一、snapshot 返回带 version=1 与 stats 的深拷贝，restore 后位图、最高序号、字节计数与统计都要回来，结构非法报 ESP_SNAPSHOT_INVALID 且不动原状态；十二、stats 固定返回 accepted、replayed、too_old、expired、byte_limited、exhausted 六个整数。

硬约束：accept、add_sa、remove_sa、set_window_size 全程持同一把逻辑锁，同一序号在并发下只能被接受一次，统计与调用次数严格相等；快照跨进程恢复后已见序号仍然判重放、窗口左沿按恢复的最高序号继续推进；每条安全关联的位图与拒绝记录都有上限，删除关联后不残留。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发收包与重启恢复自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 网络与通信安全
- type: Bugfix
- difficulty: 困难
- module: esp_replay_guard
- typeName: EspReplayGuard
