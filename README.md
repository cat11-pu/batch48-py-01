# batch48-py-safety-a-01

## 题面

权限中心这周连出单子：客户重连反复弹授权页，撤一条另一条仍生效，撤销后下游还用旧令牌；台账无上限，重启后授权归零。范围是 gsb/batch48-py-safety-a-01 的 consent_grant_registry.py，类 ConsentGrantRegistry，维护（租户、用户、客户端）记录与撤销级联。

公开接口：ConsentGrantRegistry(*, max_grants_per_tenant=100, default_ttl=86400)；grant_consent、check_consent、withdraw_consent(tenant, user, client, scopes, now)；register_token(token_id, scopes)；token_usable(token_id)；purge_expired(now)；records_for 升序；snapshot、restore、stats。拒绝抛 ConsentError(code, detail)。

故障与边界：①scope 去重字典序、版本自 1；②子集/相同集合返回原记录给 granted、版本时间不动计 suppressed，扩大必须 prompt；③扩大取并集、版本加一、时间刷新、不复活旧记录；④废弃 scope 只拒该项其余照常授予；⑤撤销级联废该用户该客户端令牌并计数；⑥撤销后再授权须出新版本；⑦过期由 purge_expired 删除计数，check 只判失效；⑧每租户活记录封顶，超限 CONSENT_CAPACITY_EXCEEDED；⑨空参数或非字符串 scope 抛 CONSENT_INVALID_REQUEST；⑩快照深拷贝，非法结构 CONSENT_SNAPSHOT_INVALID 原状态不变。

硬约束：同用户同客户端并发授权持锁合并取并集，versions 与版本号不得丢；快照恢复后记录、令牌、废弃 scope、计数都在且不共享结构；每租户硬顶。stats 返回 versions、suppressed、prompted、withdrawn、revoked_tokens、purged。

验收：`python3 -m pytest -q tests` 15 条全过；不改测试、不跳过、不写死；并发与重启自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 身份认证
- type: Bugfix
- module: consent_grant_registry
- typeName: ConsentGrantRegistry
