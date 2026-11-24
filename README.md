# batch48-py-safety-d-07

## 题面

支付网关这两个月出现了两起重复扣款工单：同一笔扣款在客户端重试后被第二次执行，日志里两个请求 nonce 相同，body 只差一个金额字段；另一次客户端把时间戳往前挪了十分钟，签名依旧通过，事后才发现校验环节根本没看时间。更隐蔽的一类问题出现在回放旧请求时：签名和 nonce 都对得上，网关把整笔查询重新跑了一遍，查重表被这些重放记录撑到两百多万行；运维重启网关后，滑窗与响应缓存又整体清空，同一批重试立刻又穿透进来。值班同学一度怀疑签名算法本身有问题，最后才定位到时间与 nonce 两条校验链各自为政。

范围是 gsb/batch48-py-safety-d-07 下的 signed_rpc_replay_guard.py，类 SignedRpcReplayGuard，负责请求签名验证、时钟偏移判定、按客户端分桶的 nonce 滑窗以及幂等响应缓存。

对外方法：`SignedRpcReplayGuard(*, clock_skew: int = 30, window: int = 300, per_client_capacity: int = 256, max_clients: int = 1000, clock=None)`；`add_client(client_id: str, secret: bytes) -> dict`；`revoke_client(client_id: str) -> None`；`sign_request(*, client_id: str, method: str, path: str, body: bytes, timestamp: int, nonce: str) -> dict`；`verify_request(request: dict, *, now=None) -> dict`；`record_response(client_id: str, nonce: str, response: dict) -> dict`；`execute_once(request: dict, handler, *, now=None) -> dict`；`nonce_window(client_id: str, *, now=None) -> list[str]`；`purge(*, now=None) -> int`；`snapshot() -> dict`；`restore(state: dict) -> None`；`stats() -> dict`；`reset_stats() -> None`。拒绝统一抛 RpcGuardError(code, detail)。

故障与边界：一、请求信封必须带 client_id、method、path、body_sha256、timestamp、nonce、signature，签名是对 method、path、body 摘要、timestamp、nonce 用换行连接的规范串做 HMAC-SHA256，signature 为十六进制字符串，body_sha256 为 body 的 sha256 十六进制值；二、未登记的 client_id 抛 UNKNOWN_CLIENT，已撤销的抛 CLIENT_REVOKED，撤销后既不能签名也不能验签；三、method、path、timestamp、nonce、body 摘要任一被改动都使签名失效抛 BAD_SIGNATURE，校验失败不占用 nonce；四、timestamp 与本地时钟偏差超过 clock_skew 抛 STALE_TIMESTAMP，恰好等于 clock_skew 放行；五、nonce 按客户端分桶，两个客户端用同一个 nonce 互不影响；六、同一客户端同一 nonce 在窗口内再次提交且 body 摘要不同抛 NONCE_CONFLICT；七、nonce 容量满时拒绝新请求抛 NONCE_FULL，不允许淘汰仍在窗口内的活跃记录来腾位置；八、execute_once 校验通过后先占位再调用 handler，成功后把结果记入响应缓存，同一 nonce 并发提交只执行一次，其余请求等待并拿到缓存中的同一结果，不得抛异常；九、purge 只回收 now 减去 timestamp 大于 window 的记录并返回条数；十、timestamp 回退到旧窗口的请求按自己的时间落桶，不与当前桶合并；十一、签名、校验、执行、落缓存全链路持锁，verified、rejected、replayed、conflicts、executed 五个计数与实际调用次数一致；十二、快照覆盖客户端密钥、nonce 滑窗与响应缓存，恢复后旧窗口与缓存仍可用，结构非法抛 STATE_INVALID 且原状态不变。

接口返回约定：add_client 返回含 client_id 的登记记录；execute_once 返回 status、nonce 与 response；verify_request 成功返回 status、client_id、nonce 与 replayed；nonce_window 按时间先后返回窗口内的 nonce 列表；校验顺序固定为 UNKNOWN_CLIENT、CLIENT_REVOKED、BAD_SIGNATURE、STALE_TIMESTAMP、NONCE_CONFLICT。

硬约束：八路并发提交同一 nonce 时 handler 只能被调用一次，计数不得多算；滑窗与响应缓存按 window 滚动回收，每个客户端容量硬上限，禁止无界增长；snapshot 必须是深拷贝，恢复后的对象不得与原状态共享可变结构；执行入口与校验共用一把可重入锁，避免占位与执行之间出现空档。stats 固定返回上述五个整数，reset_stats 归零。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发与重启恢复自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 网络与通信安全
- type: Bugfix
- difficulty: 地狱
- module: signed_rpc_replay_guard
- typeName: SignedRpcReplayGuard
