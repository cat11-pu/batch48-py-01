# batch48-py-safety-a-30

## 题面

边缘节点打开 TCP Fast Open 后，工单集中到 cookie 这一块：三天前的 cookie 还能在 SYN 里提前带数据，服务端照单全收；密钥轮换当天旧 cookie 时好时坏，值班只能把开关整个关掉；攻击者拿别的源地址的 cookie 重放，服务端照样放行并把 SYN 数据交给了应用；同一地址短时间内被刷几千次签发，cookie 生成把 CPU 打满；节点重启后当前密钥与宽限期全丢，客户端被迫重新握手；cookie 里算进源端口，同一客户端换端口就被当成新地址。范围是 gsb/batch48-py-safety-a-30 下的 tfo_cookie_guard.py，类 TfoCookieGuard，负责 cookie 签发、密钥轮换双读、地址绑定与失败回退。

公开接口：TfoCookieGuard(*, cookie_ttl_ms: int = 60000, key_grace_ms: int = 30000, rate_limit: int = 3, rate_window_ms: int = 1000)；rotate_key(key_id: str, *, now_ms: int = 0) -> dict；revoke_key(key_id: str) -> None；issue(addr: str, *, now_ms: int = 0) -> dict；verify(addr: str, cookie: str, *, now_ms: int = 0) -> dict；normalize_addr(addr: str) -> str；sfo_deliverable(addr: str, cookie: str, *, now_ms: int = 0) -> bool；list_keys() -> list；snapshot() -> dict；restore(state: dict) -> None；stats() -> dict；reset_stats() -> None。拒绝统一抛 TfoError(code)，code 取 TFO_INVALID_ADDRESS、TFO_NO_KEY、TFO_DUPLICATE_KEY、TFO_UNKNOWN_KEY、TFO_RATE_LIMITED、TFO_SNAPSHOT_INVALID。

要修的点：一、没有可用密钥（从未轮换或当前代次已撤销）时签发抛 TFO_NO_KEY，rotate_key 登记新代次并返回 key_id、generation、previous，generation 从 1 起递增，previous 为上一代 key_id 或 None，重复 key_id 抛 TFO_DUPLICATE_KEY，密钥表始终只保留当前与上一代，更早的代次不可再验签；二、issue 返回 cookie、key_id、expires_ms，cookie 固定为 32 字节的 64 位小写十六进制串，由 8 字节密钥代次、8 字节到期毫秒与 16 字节 HMAC 标签依次拼接，expires_ms 等于 now_ms 加 cookie_ttl_ms，同一地址同一 now_ms 重复签发得到相同 cookie；三、verify 不因 cookie 非法而抛错，固定返回 ok、key_id、reason 三项，reason 取 ok、malformed、address_mismatch、expired、bad_signature、key_revoked、key_unknown，now_ms 达到或超过 expires_ms 即判过期，签发地址与校验地址不一致判 address_mismatch，改任一十六进制位判 bad_signature，长度或字符不合法判 malformed；四、cookie 标签覆盖归一化后的地址、密钥代次与到期时间，用该代次派生的密钥计算，地址绑定覆盖 IPv4-mapped IPv6 且与源端口无关，同地址不同端口算同一客户端，签发地址非法抛 TFO_INVALID_ADDRESS；五、轮换后新签发只用当前代次，上一代密钥在新密钥轮换时刻加 key_grace_ms 之前仍可验签，超过宽限期后用旧密钥签发的 cookie 必须拒绝而不是继续双读；六、cookie 有效期与密钥宽限期必须同时满足，只满足其一的要拒绝，revoke_key 立即失效，撤销优先于宽限期，验签返回 key_revoked，撤销未知 key_id 抛 TFO_UNKNOWN_KEY；七、每个归一化地址在 rate_window_ms 内最多签发 rate_limit 次，超出抛 TFO_RATE_LIMITED，被限速的调用计入 rejected 与 rate_limited，窗口按绝对毫秒滑动，过期记录要淘汰，不同地址互不影响；八、sfo_deliverable 只有在 verify 成功时返回 True，校验失败的 SYN 数据不得提前交付应用，只能回退常规三次握手；九、list_keys 按 key_id 升序返回 key_id、generation、state、grace_until_ms 四项，state 取 active、previous、revoked，只有 previous 的 grace_until_ms 有值，revoked 的为 None；十、快照固定带 version=1、keys、rate、stats 四个键，keys 与 rate 分别以 key_id 和归一化地址为键，必须是深拷贝，restore 后密钥代次、宽限期、限速记录与统计都回来，宽限期按绝对毫秒继续计算，结构非法抛 TFO_SNAPSHOT_INVALID 且原状态不变；十一、stats 固定返回 issued、verified、accepted、rejected、rotated、rate_limited 六个整数，issued 计签发成功、accepted 计验签通过、rejected 计验签失败与限速拒绝、rotated 计轮换成功、rate_limited 计限速拒绝，reset_stats 全部归零。

硬约束：rotate_key、revoke_key、issue、verify、snapshot、restore 全程持同一把逻辑锁，八路并发签发与验签时 issued、verified、accepted 必须分别等于成功调用次数，不允许丢计数或重复计数；快照跨进程恢复后上一代密钥的宽限期按绝对毫秒继续，不重新计时；限速窗口与密钥表必须有界，过期记录和更早代次不得无界堆积。

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
- module: tfo_cookie_guard
- typeName: TfoCookieGuard
