# batch48-py-safety-d-15

## 题面

远程桌面会话之间能互相粘贴，运维在管理会话里复制的口令被带进了普通用户会话；剪贴板里的大段内容一直留到下一次登录，锁屏之后还能粘出来。更糟的是大文件被反复粘贴，单个会话的剪贴板涨到几十兆，没人知道该清哪一条。值班同事只能挨个踢会话，既不知道哪条内容是敏感标记，也说不清每个会话到底压了多少字节。范围是 gsb/batch48-py-safety-d-15 下的 clipboard_isolation_guard.py，类 ClipboardIsolationGuard，负责按会话隔离的剪贴板：条目带所有者会话、用户、MIME、大小与敏感标记，读取与粘贴按会话归属和策略授权。

公开接口：`ClipboardIsolationGuard(*, max_entries: int = 8, max_bytes: int = 4096, default_ttl: int = 300, clock=None)`；`open_session(session, *, user) -> dict`；`write(session, payload, *, mime="text/plain", sensitive=False, pinned=False, ttl=None, now=None) -> dict`；`read(session, entry_id, *, now=None) -> dict`；`paste(source, target, entry_id, *, now=None) -> dict`；`list_entries(session, *, now=None) -> list`；`lock_session(session) -> int`；`unlock_session(session) -> dict`；`logout(session) -> int`；`expire(*, now=None) -> int`；`snapshot() -> dict`；`restore(state: dict) -> None`；`stats() -> dict`；`reset_stats() -> None`。时间参数统一用整秒时间戳，省略时取注入的 clock 或系统当前时间；条目编号按池内顺序分配，快照恢复后继续递增。

故障与边界：一、未登记会话抛 CLIPBOARD_SESSION_INVALID，已注销会话抛 CLIPBOARD_SESSION_CLOSED，会话名区分大小写；二、payload 为空、不是 str 或 bytes 抛 CLIPBOARD_PAYLOAD_INVALID，字节数按 UTF-8 编码计算，mime 为空串同样拒绝；三、条目不存在抛 CLIPBOARD_ENTRY_NOT_FOUND，读取不属于当前会话的条目抛 CLIPBOARD_CROSS_SESSION_DENIED；四、跨会话粘贴要求源会话与目标会话属于同一用户，不同用户抛 CLIPBOARD_CROSS_SESSION_DENIED；五、条目在 expires_at 到点后不可读，读取抛 CLIPBOARD_ENTRY_EXPIRED，expire 统一回收到期条目并返回删除条数；六、每会话条数或总字节超限时按最旧、非钉住顺序淘汰，钉住条目只免于淘汰、不豁免容量统计，没有可淘汰条目时抛 CLIPBOARD_QUOTA_EXCEEDED 且不写入；七、锁定会话后读写与粘贴都抛 CLIPBOARD_SESSION_LOCKED，解锁后恢复；八、paste 生成新条目，pasted_from 指向源条目，新条目默认不钉住、到期时间按目标会话重新计算，复制后两者不共享可变对象；九、snapshot 只保存会话表、条目元数据与计数，restore 缺任一部分抛 CLIPBOARD_SNAPSHOT_INVALID 且原状态不变；十、stats 固定返回 written、read、pasted、denied、expired、evicted 六个整数，所有被拒路径都要让 denied 增长，reset_stats 全部归零；十一、list_entries 按写入顺序返回元数据，不含 payload，也不返回已到期条目。

硬约束：敏感条目跨会话粘贴一律拒绝，即使两个会话属于同一用户；会话锁定或注销时清空该会话全部条目，包括未到期的钉住条目；快照不得保存 payload 内容，恢复后只能看到元数据；write、read、paste、expire 全程持锁，八路并发各写十条后 written 必须等于 80，条目标识不得重复也不得丢失，每个会话恰好保留十条。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发与超时清空场景自行验证；只做本地实现，不涉及上游提交。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 系统安全
- type: Bugfix
- difficulty: 地狱
- module: clipboard_isolation_guard
- typeName: ClipboardIsolationGuard
