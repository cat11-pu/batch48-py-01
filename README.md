# batch48-py-safety-d-10

## 题面

营销系统切换到新域名之后，发出去的邮件在收件方批量验签失败：同一封信在主域能过，在新域就不过，运维比对半天才发现头部规范化方式一个是 relaxed、一个是 simple，混用之后头部顺序对不上；还有一封信带了 l= 签名，攻击者往正文尾部追加了一段推广内容，验签仍然通过。更严重的是选择器密钥轮换那天，旧签名在业务方侧直接失效，对方要求至少保留一天宽限，实际连一小时都没撑住。

范围是 gsb/batch48-py-safety-d-10 下的 dkim_signature_guard.py，类 DkimSignatureGuard，负责头部与正文规范化、正文哈希、签名生成与验签、选择器密钥轮换。

对外方法：`DkimSignatureGuard(*, max_keys: int = 64, clock=None)`；`add_selector_key(domain: str, selector: str, private_key: bytes, *, algorithm: str = "hmac-sha256", not_before=None, expires=None, now=None) -> dict`；`rotate_selector(domain: str, selector: str, private_key: bytes, *, grace_seconds: int = 86400, now=None) -> dict`；`sign(*, domain: str, selector: str, headers: list, body: bytes, signed_headers=(), canonicalization: str = "relaxed", header_modes: dict | None = None, length: int | None = None, now=None) -> dict`；`verify_message(message: dict, *, now=None) -> dict`；`canonicalize_header(name: str, value: str, mode: str) -> bytes`；`canonicalize_body(body: bytes, *, mode: str = "relaxed", length: int | None = None) -> bytes`；`body_hash(body: bytes, *, length: int | None = None, mode: str = "relaxed") -> str`；`selector_state(domain: str, selector: str, *, now=None) -> dict`；`expire_selectors(*, now=None) -> int`；`snapshot() -> dict`；`restore(state: dict) -> None`；`stats() -> dict`；`reset_stats() -> None`。拒绝统一抛 DkimError(code, detail)。

故障与边界：一、头部规范化分 simple 与 relaxed，simple 保留字段名大小写与原始空白，relaxed 小写字段名、展开折行、把连续空白压成一个空格并去掉值两端空白，两种模式的结果都是字段名加冒号加值再加 CRLF；二、同一封邮件的不同已签名头可以各自声明规范化方式，按各自声明逐头处理，不能统一折叠；三、正文 relaxed 规范化压缩行内空白、去掉行尾空白与尾部空行、结尾保留单个 CRLF，simple 保持字节原样，空正文按单个 CRLF 处理；四、body_hash 对规范化正文算 sha256 并以十六进制字符串返回，带 l= 时只覆盖前 l 字节，实际正文短于 l 时验签必须失败；五、签名覆盖的头部顺序由 signed_headers 给定的顺序决定，From 必须被签名覆盖，否则抛 DKIM_FROM_REQUIRED；六、选择器按域与 selector 管理，未知选择器抛 DKIM_UNKNOWN_SELECTOR，先查密钥再做后续校验；七、算法只支持 hmac-sha256，其他算法登记时抛 DKIM_UNSUPPORTED_ALGORITHM；八、规范化方式非法抛 DKIM_CANON_INVALID；九、已签名头被改动抛 DKIM_INVALID_SIGNATURE，正文被改动抛 DKIM_BODY_HASH_MISMATCH，未签名的头可以自由变化；十、选择器轮换后新一代立即生效，旧签名在宽限期内仍可验，宽限期结束立即拒绝；十一、签名结果确定、不含随机分量，签名头字段按固定顺序输出，默认使用最新代次；十二、快照覆盖选择器密钥与宽限期信息，恢复后仍可签名验签，结构非法抛 DKIM_SNAPSHOT_INVALID 且原状态不变。

接口返回约定：签名块返回 domain、selector、generation、canonicalization、signed_headers、body_hash 与 signature；verify_message 成功返回 status、domain、selector、generation 与 signed_headers；selector_state 返回 generation，rotate_selector 返回 generation 与 grace_until。

硬约束：签名与轮换串行执行，八路并发签名同一封邮件后 8 份签名必须完全一致且验签全部通过，signed 计数恰好 8；已签名头与签名字段顺序不得因并发漂移；snapshot 必须是深拷贝，恢复后的对象不得与原状态共享可变结构；stats 固定返回 signed、verified、rejected 三个整数，reset_stats 归零。

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
- module: dkim_signature_guard
- typeName: DkimSignatureGuard
