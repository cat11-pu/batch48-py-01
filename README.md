# batch48-py-safety-a-18

## 题面

证书轮换压测之后，网关侧的 TLS 1.3 连接开始零星断流：对端刚发了 KeyUpdate，本地立刻换了发送密钥，可旧代里还有几条应用数据没写完，对端按旧代解不开直接告警；另一些连接在密钥更新后收到迟到的旧代记录，解密模块把它们当成新代重放丢掉，重传越积越多。压测还发现连续 KeyUpdate 没有节流，一条连接能在一秒内把代次推高十几代，旧密钥保留窗口被冲垮；同一代序号用满之后仍然有记录被塞进来，序号空间被复用却没有报错，审计日志里只留下一个含糊的丢弃计数。

范围是 gsb/batch48-py-safety-a-18 下的 key_phase_guard.py，类 KeyPhaseGuard，负责收发密钥代次切换、旧代保留窗口、每代序号空间与更新限速。

公开接口：`KeyPhaseGuard(*, seq_limit=8, max_updates=4, min_update_interval_ms=500, retain_epochs=1, retain_ms=2000, clock=None)`；`start(*, now=0) -> dict`；`key_phase() -> dict`；`request_update(*, now=0) -> dict`；`on_key_update(*, now=0) -> dict`；`encrypt(seq, *, now=0) -> dict`；`decrypt(epoch, seq, *, now=0) -> dict`；`track_write(seq, *, now=0) -> dict`；`ack_write(epoch, seq) -> dict`；`pending_writes() -> int`；`expire(*, now=0) -> dict`；另有 stats、reset_stats、snapshot、restore。拒绝统一抛 KeyPhaseError(code, detail)。

故障与边界：一、未 start 前调用 encrypt、decrypt、更新或 key_phase 报 KP_NOT_STARTED，重复 start 报 KP_ALREADY_STARTED；二、序号取 0 到 seq_limit-1 的整数，负数、越界、布尔与非整数报 KP_SEQ_OUT_OF_RANGE，同一代内发送序号必须严格递增，重复或回退报 KP_SEQ_REUSED；三、request_update 推进接收代次并把上一代放进保留窗口，保留到期时间记在 key_phase 里；四、连续更新受最小间隔与总次数双重限制，太快报 KP_UPDATE_TOO_SOON，次数用尽报 KP_UPDATE_LIMIT，两者都要计入 updates_rejected；五、on_key_update 只切换发送代次，新代从零开始序号空间；六、decrypt 对未来代次报 KP_EPOCH_UNKNOWN，对保留窗口外或已过期的旧代返回 discarded 与 KP_KEY_EXPIRED 并计入 discarded，expire 清掉超过保留时限的旧代并返回被清代次，两者都不能抛异常；七、同一代重复序号在接收侧报 KP_SEQ_REUSED；八、同一代序号空间用尽后再收到该代记录必须报错，不能静默丢弃后复用序号；九、保留窗口内旧代记录解密成功后，旧代序号不得计入新代重放窗口；十、KeyUpdate 与在途应用数据交错时，track_write 登记的在途记录未 ack 完之前不得提前切换发送密钥，on_key_update 要返回延迟状态并由 ack_write 收尾提交；十一、snapshot 是深拷贝，restore 结构非法报 KP_SNAPSHOT_INVALID 且原状态不变。

硬约束：start、request_update、on_key_update、encrypt、decrypt、ack_write 全程持锁，四路并发解密同一代的不同序号时，成功计数必须与调用次数一致，重放集合不能丢序号也不能重复计数；旧代保留按代次与时间双重判定，过期判定必须用传入的 now，不依赖墙钟；更新间隔、更新次数与保留代数都是硬上限，超限拒绝而不是排队；快照恢复后代次、保留窗口、已见序号、发送序号与统计都要在，恢复后继续按代次去重，且不得与恢复前的对象共享结构。

验收：`python3 -m pytest -q tests` 的 15 条必须全部通过；不得修改测试、跳过或按固定输入写死；并发与重启恢复场景自行验证；只做本地实现，不涉及上游提交。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- name: batch48-py-safety-a-18
- domain: 安全
- sub: 网络与通信安全
- type: Bugfix
- difficulty: 困难
- module: key_phase_guard
- typeName: KeyPhaseGuard
