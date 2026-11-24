# batch48-py-safety-b-04

## 题面

共享存储上的索引文件上周被两个进程同时写，收尾时对不上账；排查发现租约明明过期了，续约接口还能把它续上；排队的写进程等了半小时一直没拿到名额，日志里显示它前面那个请求早就取消了，却还占着队首。范围是 gsb/batch48-py-safety-b-04 下的 file_lease_coordinator.py，类 LeaseCoordinator，负责资源键上的排他与共享租约的授予、续约、释放、过期回收与等待队列。

公开接口：LeaseCoordinator(*, max_leases=1024)；acquire(key, owner, mode, ttl, now)；renew(lease_id, now, ttl=None)；release(lease_id, owner)；holder(key)；waiters(key)；cancel_wait(key, owner)；expire(now)；stats()；snapshot()；restore(state)。租约用 Lease(lease_id, key, owner, mode, acquired_at, expires_at) 承载，拒绝统一抛 LeaseError(code, detail)。holder 返回按租约号升序的字典列表，字段为 lease_id、owner、mode、expires_at；waiters 按排队先后返回 owner、mode、seq。

故障与边界：一、acquire 校验 key 与 owner 非空、mode 只能是 shared 或 exclusive、ttl 为正整数、now 为非负整数，非法分别报 LEASE_INVALID_REQUEST 与 LEASE_INVALID_TTL；二、同一资源键上 exclusive 与任何其他租约互斥，冲突时不授予，按到达顺序排进等待队列并报 LEASE_BUSY，同一 owner 在队列里不重复占位；三、同一 owner 对同一 key 用同一模式重复 acquire 幂等，返回原租约且不刷新到期时刻，不得生成第二条；四、shared 租约之间可以共存，两个读者能同时拿到同一文件；五、shared 持有者申请 exclusive 不得就地升级，必须排到现有持有者之后并等待；六、release 必须带 owner，不符报 LEASE_OWNER_MISMATCH 且租约仍然有效，未知租约报 LEASE_UNKNOWN_LEASE，释放成功后只授予队首一个等待者；七、expire 按到期时刻判定、与插入顺序无关，now 等于到期时刻即算到期，返回按租约号升序排好的列表，回收后同样只授予队首；八、renew 把到期时刻推到 now 加 ttl，已过期租约续约报 LEASE_EXPIRED，既不复活也不删除，留给 expire 回收；九、时钟回拨时到期比较要取单调推进量与墙钟中较小的推进量，不能因为回拨把租约无限延长；十、等待队列里被取消的请求必须从队列摘除，否则会占住队首名额，让后来者一直饿死；十一、快照保存租约、到期时刻、owner、等待队列序号与观测时刻，恢复后未过期的继续有效、已过期的不复活，非法结构报 LEASE_SNAPSHOT_INVALID 且原状态不变；十二、stats 固定返回 granted、denied、renewed、expired、released、waited 六个整数，拒绝与排队都要计数，方便值班核对争抢情况。

硬约束：acquire、release、expire 全程持锁，同一资源键在并发争抢下只能产生一个 exclusive 持有者，等待队列严格先进先出、不得插队；快照必须是深拷贝，恢复后不与调用方共享可变结构；单次授予只唤醒队首，不能把队列里所有人一起放行。

验收：`python3 -m pytest -q tests` 的 15 条测试全部通过；不得修改测试、跳过或按固定输入写死；并发争抢、时钟回拨与重启恢复场景自行验证；只做本地实现。
以上现象均在真实生产环境复现。
## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 系统安全
- type: Bugfix
- module: file_lease_coordinator
- typeName: LeaseCoordinator
