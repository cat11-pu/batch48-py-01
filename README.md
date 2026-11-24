# batch48-py-safety-d-13

## 题面

密钥解封后的缓冲区归还池子，下一个租户租出来还能读到上一家留下的密钥片段；高峰期池子自作主张扩容，把机器内存打满，进程被 OOM 杀掉，重启后密钥解封全量失败。运维翻看监控只看到内存曲线陡增，既不知道哪个租户没有归还，也说不清一共拒绝过多少次租借，最后只能靠重启把池子清空。范围是 gsb/batch48-py-safety-d-13 下的 zeroize_buffer_pool.py，类 ZeroizeBufferPool，负责固定容量敏感缓冲的租借、归还与零化。

公开接口：`ZeroizeBufferPool(capacity: int, buffer_size: int, *, retry_after_ms: int = 50)`；`acquire(size=None, *, owner: str = "") -> dict`；`release(lease) -> dict`；`release_owner(owner: str) -> int`；`zeroize_all() -> int`；`free_count() -> int`；`leased_count() -> int`；`pool_capacity() -> int`；`lease_info(lease) -> dict`；`snapshot() -> dict`；`restore(state: dict) -> None`；`stats() -> dict`；`reset_stats() -> None`。acquire 返回含 lease_id、owner、size、capacity、buffer 的租约，buffer 是可直接读写的 bytearray，默认长度取 buffer_size；lease_id 由池子统一分配，retry_after_ms 是给调用方的建议退避毫秒数；所有公开方法必须自身线程安全，调用方不需要额外加锁。

故障与边界：一、capacity 或 buffer_size 非正时构造即拒绝，抛 POOL_CAPACITY_INVALID；二、size 非正或大于 buffer_size 抛 POOL_SIZE_INVALID，不得占用槽位，也不得让 free_count 变动；三、空闲表为空时抛 POOL_EXHAUSTED，异常对象 retryable 为 True，并计入 rejected，错误里带上 retry_after_ms 建议退避时长；四、release 先把整块缓冲（含租约未使用的尾部）写零再放回空闲表，顺序不得颠倒，重复归还抛 POOL_DOUBLE_RELEASE；五、归还属于别的池子的租约抛 POOL_FOREIGN_LEASE，结构未知的租约对象抛 POOL_LEASE_INVALID；六、release_owner 按 owner 批量归还并返回归还条数，不影响其他 owner 的租约；七、zeroize_all 对所有空闲缓冲再写一遍零并返回本次写零字节数；八、lease_info 返回 owner、size、capacity 与 leased 状态，已归还的租约 leased 为 False；九、快照只导出容量、块大小与计数，是深拷贝，restore 只恢复配置与计数，缺字段或容量与构造参数不符抛 POOL_SNAPSHOT_INVALID 且原状态不变；十、stats 固定返回 acquired、released、rejected、zeroized_bytes 四个整数，zeroized_bytes 按缓冲块大小累计写零字节数，reset_stats 全部归零；十一、池容量在构造时固定，运行期不得扩容，也不得静默丢弃租约；十二、free_count 加 leased_count 必须始终等于容量，统计与这两个数字要能互相对上。

硬约束：acquire、release、release_owner 全程持锁，八路并发各租借归还十五次后 acquired 与 released 都必须恰好等于 120，free_count 回到容量，不得出现丢计数或槽位泄漏；归还顺序与租约归属必须可核对，禁止把某个 owner 的租约算到另一个 owner 名下；任何路径都不得把未零化的缓冲交回空闲表；零化必须覆盖整块缓冲，已归还租约的旧引用读到的也必须是零，不能只把长度截短；快照不得包含任何缓冲内容，restore 后空闲表为空，计数与快照一致；租借耗尽只返回可重试错误，由调用方退避后重来，池子自己不扩容。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发与耗尽场景自行验证；只做本地实现，不涉及上游提交。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 系统安全
- type: Bugfix
- difficulty: 地狱
- module: zeroize_buffer_pool
- typeName: ZeroizeBufferPool
