# batch48-py-safety-d-11

## 题面

证书轮换窗口里，运维把一份旧的信任根包重新推了一次，半数进程的信任库悄悄回退到过期根，新连接握手接连失败；还有几次推送在替换途中被读方撞见半套根集，连本来正常的根也验不过。值班同学翻遍日志只看到一条“安装成功”，既说不清哪次推送被拒，也讲不出进程里现在到底是哪一版根集。范围是 gsb/batch48-py-safety-d-11 下的 trust_store_update_guard.py，类 TrustStoreUpdateGuard，负责签名增量包校验、版本管理与根集切换。

公开接口：`TrustStoreUpdateGuard(*, max_roots: int = 32, clock=None)`；`register_issuer(issuer_id, secret) -> None`；`revoke_issuer(issuer_id) -> None`；`install(bundle: dict, *, now=None) -> dict`；`active_roots(*, now=None) -> list`；`legacy_roots(*, now=None) -> list`；`lookup(subject: str, *, now=None) -> dict`；`current_version() -> int`；`rejection_log() -> list`；`snapshot() -> dict`；`restore(state: dict) -> None`；`stats() -> dict`；`reset_stats() -> None`。包的字段为 version、issuer、roots、digest、signature；digest 是 roots 按键排序紧凑 JSON 的 SHA-256 十六进制，signature 是 HMAC-SHA256(签发密钥, "issuer:version:digest") 的十六进制。install 成功返回 status、version、active、legacy，活动根集保持包内顺序。所有时间参数为 ISO-8601 字符串，省略时取注入的 clock 或系统当前时间；签发者密钥只在登记时传入，任何返回值和快照都不得回显密钥原文。

故障与边界：一、缺字段或类型不符（version 不是正整数、roots 不是列表、issuer 为空串、signature 长度不对）抛 TRUST_BUNDLE_MALFORMED，且不得改动当前根集；同一次拒绝只记一条，不得重复计数；二、签发者未登记或已撤销抛 TRUST_ISSUER_UNKNOWN；三、签名不符抛 TRUST_SIGNATURE_INVALID，摘要与按 roots 重算结果不一致抛 TRUST_DIGEST_MISMATCH，两类都要留下拒绝原因；四、同一包内两条根的指纹相同抛 TRUST_DUPLICATE_FINGERPRINT，防止同一条根重复登记后来源无法追溯；五、某条根 ca 字段非真、subject 为空或有效期倒挂抛 TRUST_ROOT_INVALID，整包拒绝；六、包版本必须严格大于当前版本，相等或更低一律抛 TRUST_VERSION_REGRESSION，即使内容与签名都正确；过期根不删除，转入旧根集保留，只用于验旧链，不参与 lookup；七、活动根集超过 max_roots 抛 TRUST_CAPACITY_EXCEEDED，旧根集原样保留；八、任何校验失败都保留旧版本与旧根集，并把 code 与 version 记入 rejection_log，日志按发生顺序追加；九、lookup 命中返回 subject、fingerprint、not_before、not_after、ca，未命中抛 TRUST_ROOT_NOT_FOUND，命中与未命中分别计入 lookups 与 lookups_missed；十、同一签发者换密钥抛 TRUST_ISSUER_CONFLICT，重复登记同一密钥幂等且不报错；十一、快照导出当前版本、活动根集、旧根集与计数，不含签发者密钥，restore 后需重新登记签发者才能继续安装，结构非法抛 TRUST_SNAPSHOT_INVALID 且原状态不变；十二、stats 固定返回 install_ok、install_rejected、lookups、lookups_missed 四个整数，reset_stats 全部归零。

硬约束：install、lookup 与快照全程持锁，十路并发查询同一主题时 lookups 必须与调用总次数完全相等，不得丢计数；切换只能整集生效，读方只能看到完整的旧根集或完整的新根集，不得看到新旧混合；失败安装不得污染当前版本与当前根集；快照必须是深拷贝，restore 后不得与快照共享可变结构，重启恢复后旧包仍要按版本比较被拒绝。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发与重启恢复场景自行验证；只做本地实现，不涉及上游提交。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 系统安全
- type: Bugfix
- difficulty: 地狱
- module: trust_store_update_guard
- typeName: TrustStoreUpdateGuard
