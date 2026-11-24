# batch48-py-safety-a-02

## 题面

授权码登录出事故：同一码被两个进程换出两张令牌；重试已兑换的码仍被接受；改 redirect_uri 仍能换码；换 verifier 也能过 PKCE；码表无上限，重启后未兑换码全失效。落点 gsb/batch48-py-safety-a-02 的 authcode_exchange_guard.py，类 AuthCodeExchangeGuard。

公开接口：AuthCodeExchangeGuard(*, ttl, max_codes)；issue_code(client_id, redirect_uri, challenge, method, scopes, now)；redeem(code, client_id, redirect_uri, verifier, now)；revoke_derived(code)；inspect_code(code)；token_usable(token_id)；purge_expired(now)。失败抛 AuthCodeError(code, detail)。

故障与边界（码前缀 AUTHCODE_）：①S256 全等校验；②method 为 plain 也逐字符比；③码绑 client_id、redirect_uri，不符报 CLIENT_MISMATCH 或 REDIRECT_MISMATCH 且不消费；④verifier 不符报 PKCE_MISMATCH；⑤过期报 EXPIRED，优先于重放不触发级联；⑥已消费码再兑换报 REPLAY 并作废派生令牌；⑦inspect_code 返回副本；⑧未知码报 UNKNOWN、purge_expired 回收计数；⑨容量封顶超限 CAPACITY_EXCEEDED；⑩空参数或未知 method 报 INVALID_REQUEST；⑪快照深拷贝，非法结构 SNAPSHOT_INVALID 原状态不变。

硬约束：同一码并发兑换只成功一次，其余按重放并作废已发令牌；码表容量硬顶、按到期回收；快照恢复后未兑换码可兑、作废令牌不可用且不共享结构。stats 返回 issued、redeemed、replays、revoked_tokens、purged。

验收：`python3 -m pytest -q tests` 15 条全过；不改测试、不跳过、不写死；仅本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 身份认证
- type: Bugfix
- module: authcode_exchange_guard
- typeName: AuthCodeExchangeGuard
