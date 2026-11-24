# batch48-py-safety-a-21

## 题面


配额层是防刷与防拒绝服务的关键闸门。计费网关上周切到新的配额层后，几条告警同时炸开：攻击者用并发预扣、跨窗口重放和租约泄漏把全局额度占满，正常业务被判超额；凌晨两点整批请求被连续拒绝，事后核对发现同一笔请求在整点前后各扣了一次，日结报表多扣了三成；运营手工退还额度后，第二天的用量报表出现负值，同一张单子退了两遍也没人拦住；一台机器重启后，之前预扣出去但还没结算的额度再也没回来，新请求一直撞在拒绝上，值班只能重启进程了事；并发高峰时多个线程同时预扣，总占用偶尔越过全局上限，个别 key 明明还有余量却被判超额。范围是 gsb/batch48-py-safety-a-21 下的 quota_window_limiter.py，类 QuotaWindowLimiter，负责按 key、租户、全局三层做滑动窗口计数，接入方通过它预扣、结算与归还额度。

公开接口：QuotaWindowLimiter(*, window_seconds: int = 10, key_limit: int = 100, tenant_limit: int = 500, global_limit: int = 1000, lease_ttl: float = 5.0, max_leases: int = 1000, clock=None)；reserve(key: str, tenant: str, cost: int = 1, now=None) -> str；settle(lease_id: str) -> dict；refund(lease_id: str) -> int；expire_leases(now) -> int；remaining(key: str, tenant: str, now=None) -> dict；lease_count() -> int；snapshot() -> dict；restore(state: dict) -> None；stats() -> dict。拒绝统一抛 QuotaError(code)，code 取 QUOTA_INVALID_ARGUMENT、QUOTA_EXCEEDED_KEY、QUOTA_EXCEEDED_TENANT、QUOTA_EXCEEDED_GLOBAL、QUOTA_UNKNOWN_LEASE、QUOTA_LEASE_LIMIT、QUOTA_SNAPSHOT_INVALID。

要修的点：一、三层限额任意一层不够都要拒绝并报出具体层，三层扣减要么全成要么全不成，失败不得留下部分计数；二、窗口按 window_seconds 切分，上一窗口用量按剩余时间比例折算，一次预扣只落进一个窗口；三、预扣返回租约号，settle 返回含 key、tenant、cost 的摘要，用量不变、租约消失，重复 settle 报 QUOTA_UNKNOWN_LEASE；四、refund 把额度退回原窗口且同一租约只生效一次，已用计数不得被退成负数；五、租约到 lease_ttl 超时，expire_leases 只回收到期未结算的租约并计入泄漏；六、剩余额度按当前时刻折算窗口，返回 key、tenant、global 三个整数；七、cost 必须为正整数，key、租户为空或窗口、限额非正都报非法参数；八、租约数达到 max_leases 后拒收新预扣；九、snapshot 返回带 version=1 与 stats 的深拷贝，restore 后计数、租约、统计都要回来，结构非法报 QUOTA_SNAPSHOT_INVALID 且不动当前状态；十、恢复后仍未结算的租约必须按原到期时间继续可回收，不能永久占额；十一、stats 固定返回 reserved、settled、refunded、leaked、denied 五个整数，其中 denied 只在三层判超额时增加；十二、lease_count 只统计未结算、未归还、未过期的租约，三种回收动作之后立即减少；十三、remaining、settle、refund 与 reserve 在同一 key 上必须看到同一份窗口快照，中间态不得对外可见。

硬约束：reserve、settle、refund、expire_leases 全程持同一把逻辑锁，并发预扣看到一致余量，任何时刻三层占用都不超过各自上限，统计与调用次数严格相等；快照跨进程恢复后不得与恢复前的对象共享可变结构；窗口桶与租约表有上限，旧桶不得无界增长。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发预扣、租约过期与重启恢复都要自行验证；只做本地实现，不改动上游。
## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 网络与通信安全
- type: Bugfix
- difficulty: 困难
- module: quota_window_limiter
- typeName: QuotaWindowLimiter
