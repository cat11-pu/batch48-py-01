# batch48-py-safety-a-08

## 题面

CSRF 复测：令牌在预取时被烧掉，重启后已消费令牌又可用。范围 gsb/batch48-py-safety-a-08 的 csrf_token_guard.py，类 CsrfTokenGuard，错误码 CsrfError。

接口：create_session(session_id, now)、issue_token(session_id, action, origin, one_time, now)、verify_token(session_id, token, action, origin, cookie_token, method, now)、rotate_session_token(session_id, now)、revoke_for_session(session_id)、purge_expired(now)。

故障：①create_session 重复抛 CSRF_SESSION_EXISTS，回 epoch、session_token；②issue 给 token，过期抛 CSRF_TOKEN_EXPIRED，purge 删到期计数；③cookie 不符抛 CSRF_DOUBLE_SUBMIT_MISMATCH；④未知 token 抛 CSRF_TOKEN_UNKNOWN，来源错抛 CSRF_ORIGIN_MISMATCH；⑤一次性令牌重复用抛 CSRF_TOKEN_CONSUMED，可复用令牌可重复用；⑥表满清到期，仍满抛 CSRF_TOKEN_CAPACITY；⑦revoke 清空返回条数，重复 0，再操作抛 CSRF_SESSION_REVOKED；⑧rotate 推进 epoch 换 session_token；⑨GET 等安全方法不消费一次性令牌；⑩轮换后旧令牌失效；⑪同一令牌换动作拒绝；stats 含 issued/verified/rejected/consumed/rotated/revoked。

硬约束：并发校验一次性令牌只成功一次；snapshot 含 sessions、stats 且深拷贝，restore 保留消费状态与 epoch，非法结构抛 CSRF_SNAPSHOT_INVALID。

验收：python3 -m pytest -q tests 15 条过；不改测试、不跳过、不写死；并发与重启自验。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 身份认证
- type: Bugfix
- module: csrf_token_guard
- typeName: CsrfTokenGuard
