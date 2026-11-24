# batch48-py-safety-a-27

## 题面

接入网关按 QUIC 连接 ID 做四层转发之后，故障单开始集中出现：一台 worker 下线，客户端仍然拿着旧 CID 发包，网关把包直接静默丢掉，客户端只能等到超时才重传；后来补了无状态重置，可退役期还没过就停发了，对端一直在重传同一条连接；有人从客户端自选 CID 的入口塞进一个很短的标识，绕过了长度限制和退役表检查；某台 worker 的 CID 表被打满后新连接全部失败，容量告警到最后一刻才报；网关重启后路由表和退役表一起清空，正在退役的连接全被当成陌生包丢弃，排障时连一个可核对的计数都没有。范围是 gsb/batch48-py-safety-a-27 下的 cid_route_guard.py，类 CidRouteGuard，负责 CID 分配与路由、退役管理、无状态重置令牌和每 worker 容量。

公开接口：CidRouteGuard(*, cid_len: int = 8, worker_capacity: int = 64, retire_grace_ms: int = 5000, reset_key: bytes = b"")；allocate(worker: str, *, now_ms: int = 0, client_cid: str | None = None) -> dict；route(dcid: str, *, now_ms: int = 0) -> dict；retire(cid: str, *, now_ms: int = 0) -> dict；reset_token(cid: str) -> str；verify_token(cid: str, token: str) -> bool；parse_cid(cid: str) -> dict；worker_stats(worker: str) -> dict；snapshot() -> dict；restore(state: dict) -> None；stats() -> dict；reset_stats() -> None。拒绝统一抛 CidError(code)，code 取 CID_INVALID_ARGUMENT、CID_BAD_PREFIX、CID_CLIENT_LEN、CID_RETIRED_CONFLICT、CID_IN_USE、CID_WORKER_FULL、CID_UNKNOWN、CID_SNAPSHOT_INVALID。

要修的点：一、生成的 CID 是 cid_len 字节的小写十六进制串，首字节为其余字节异或后再与 0xA5 异或的自校验字节，后面是单调递增的分配序号；parse_cid 返回 version、serial、valid 三项，长度不符、含非十六进制字符或校验字节不对都抛 CID_BAD_PREFIX；二、allocate 返回 cid、worker、token、created_ms 四个字段，token 必须等于 reset_token(cid)，同一 guard 内分配的 CID 不重复，序号严格递增；三、route 对活动 CID 返回 action 为 forward，带上 cid 与 worker，forwarded 加一；四、retire 只接受活动 CID，返回 cid、worker、retire_until_ms，截止时刻等于 now_ms 加 retire_grace_ms，退役中的 CID 不占 worker 容量，活动计数相应减少；五、退役期内的 CID 到达时 route 返回 action 为 reset，token 等于 reset_token(cid)，退役期结束前不得停发无状态重置，否则对端会一直重传；六、退役期已过的 CID 与从未分配过的 CID 要分开处理，前者回到未分配状态，后者直接丢弃，且都不计入连接统计；七、客户端自选 CID 只在 8 到 20 字节之间允许，超出范围抛 CID_CLIENT_LEN，与退役表中的 CID 冲突抛 CID_RETIRED_CONFLICT，同一 CID 短时间内在两个 worker 注册时先注册者胜，后者收到 CID_IN_USE 而不是覆盖；八、worker 名为空抛 CID_INVALID_ARGUMENT，每个 worker 的活动 CID 数达到 worker_capacity 后拒绝新分配并抛 CID_WORKER_FULL，退役中的不占额度，其他 worker 不受影响，worker_stats 返回 worker、active、retired、capacity 四项；九、reset_token 返回 16 字节令牌的 32 位十六进制串，用 reset_key 对 CID 做 HMAC 派生，同一 CID 与同一密钥结果稳定，不同 CID 或不同密钥结果不同，verify_token 对伪造令牌返回 False 而不是抛错；十、快照固定带 version=1、active、retired、serial、stats 五个键，active 与 retired 以 CID 为键，必须是深拷贝，restore 后活动路由、退役截止时刻、序号与统计都要回来，结构非法抛 CID_SNAPSHOT_INVALID 且原状态不变；十一、stats 固定返回 allocated、forwarded、resets、retired、rejected 五个整数，其中 allocated、forwarded、resets、retired 分别按成功分配、转发、重置、退役次数累加，reset_stats 全部归零，抛错的调用只增加 rejected，不计入其他计数。

硬约束：allocate、route、retire、snapshot、restore 全程持同一把逻辑锁，八路并发分配时 allocated 必须等于成功调用次数，CID 不重复、序号不重号；快照跨进程恢复后退役截止时刻按绝对毫秒继续，不重新计时；每 worker 的活动 CID 严格受 worker_capacity 封顶，退役表也不得无界增长。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发与重启恢复场景自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 网络与通信安全
- type: Bugfix
- difficulty: 地狱
- module: cid_route_guard
- typeName: CidRouteGuard
