# batch48-py-safety-a-24

## 题面

视图中继上线后，客服和值班一起排查三类现象：某个慢速下载把整条出口带宽吃光，其他会话的响应时间从几十毫秒涨到十几秒；另一条连接已经被标记暂停读取，上游却还在往缓冲区里送数据，缓冲区一路涨到内存告警，进程最后被杀；连接断开之后，租户配额仍然显示被占满，新连接全部被拒，直到重启才恢复；监控里的暂停和恢复次数对不上，同一连接反复抖动时，某个会话总能抢到更多带宽。范围是 gsb/batch48-py-safety-a-24 下的 backpressure_guard.py，类 BackpressureGuard，为每条上下游连接维护独立的高低水位、缓冲上限与租户配额，出口按 DRR 轮转出队。

公开接口：BackpressureGuard(*, high_watermark: int = 8192, low_watermark: int = 2048, hard_cap: int = 65536, grace: float = 30.0, quantum: int = 1000, tenant_quota: int = 1 << 20, clock=None)；register_conn(conn_id: str, tenant: str) -> dict；list_conns() -> list；on_read(conn_id: str, nbytes: int, now=None) -> bool；drain(conn_id: str, nbytes: int, now=None) -> int；read_paused(conn_id: str) -> bool；pending_bytes(conn_id: str) -> int；schedule(budget_bytes: int, now=None) -> list；expire_slow(now) -> list；close_conn(conn_id: str) -> int；tenant_usage(tenant: str) -> int；snapshot() -> dict；restore(state: dict) -> None；stats() -> dict。拒绝统一抛 BackpressureError(code)，code 取 BP_INVALID_ARGUMENT、BP_DUPLICATE_CONN、BP_UNKNOWN_CONN、BP_TENANT_QUOTA、BP_HARD_CAP、BP_SNAPSHOT_INVALID。

要修的点：一、on_read 按字节累计缓冲，达到高水位就把该连接置为暂停并记一次暂停，返回值表示调用结束后的暂停状态，每条连接各自计数、上下行互不影响；二、drain 只释放请求的字节，剩余缓冲降到低水位及以下才恢复读取并记一次恢复；三、暂停标记不保证上游不再到货，暂停窗口内已经到达的数据仍按实际缓冲字节判定是否越限，不能凭标记直接放行；四、单次读入后缓冲超过 hard_cap 当场断开连接并报 BP_HARD_CAP；五、同一租户所有连接的缓冲合计超过 tenant_quota 时拒收，且不改动已有缓冲；六、schedule 按注册顺序做 DRR，每轮每条连接最多出队 quantum 字节，总量到达 budget 即停，积压多的流不能饿死其他流；七、同一连接反复暂停恢复时，公平队列的虚拟时间不得回退，否则该流会长期占优；八、暂停时间超过 grace 的连接由 expire_slow 断开并按注册顺序返回连接号；九、close_conn 返回回收的字节数，重复关闭报 BP_UNKNOWN_CONN；十、断开慢消费者时其占用额度必须立刻归还租户配额，不能等后台回收；十一、低水位不低于高水位、高水位超过硬顶、宽限或配额非正都报非法参数，连接号或租户为空、重复登记、未知连接各有对应错误码；十二、snapshot 带 version=1 与 stats，restore 后队列、暂停标记、统计都要回来，结构非法报 BP_SNAPSHOT_INVALID 且不动原状态，stats 固定返回 accepted_bytes、drained_bytes、pauses、resumes、closed 五个整数。

硬约束：on_read、drain、schedule、expire_slow、close_conn 全程持同一把逻辑锁，并发读入时同一租户的缓冲合计不超过配额，统计字节与入队出队严格相等；快照跨进程恢复后不得与恢复前的对象共享可变结构；硬顶是硬边界，越限当场断开而不是淘汰已有数据。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发暂停恢复与重启恢复自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 网络与通信安全
- type: Bugfix
- difficulty: 困难
- module: backpressure_guard
- typeName: BackpressureGuard
