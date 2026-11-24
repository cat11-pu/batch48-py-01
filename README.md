# batch48-py-safety-a-06

## 题面

推送确认上线后工单不断：连点两次登录弹两次确认，后台只认第一笔；重启后锁定名单清空。范围 gsb/batch48-py-safety-a-06 的 mfa_push_orchestrator.py，类 MfaPushOrchestrator，拒绝抛 MfaPushError(code, detail)。

接口：MfaPushOrchestrator(*, ttl=120, max_failures=3, lock_seconds=300, max_challenges=500)、create_challenge(user_id, now)、submit_response(challenge_id, code, decision, now)、cancel_challenge、poll_status(challenge_id, now)、expire_stale(now)。

故障：①创建返回两位 code、expires_at、attempts，新挑战计 pushed；②未过期 pending 存在时重复创建返回原挑战标 reused；③对码 approve 清失败计数，码错分抛 MFA_CODE_MISMATCH 与 MFA_CODE_INVALID；④失败达 max_failures 锁定，locked_until 为末次失败加 lock_seconds；⑤expire_stale 把到期 pending 转 expired 计条数；⑥cancel 只作用于 pending，越界抛 MFA_INVALID_TRANSITION；⑦未知 id 抛 MFA_UNKNOWN_CHALLENGE；⑧点拒绝后轮询立即 denied；⑨迟到 approve 不翻转不计成功；⑩并发创建后写者回滚返回既有挑战；stats 键为 pushed/approved/failed/expired/cancelled/locked。

硬约束：并发提交同一挑战只允许一个成功；snapshot 含 version、challenges、users、stats 且深拷贝，restore 后锁定与计数可用，非法结构抛 MFA_SNAPSHOT_INVALID。

验收：python3 -m pytest -q tests 15 条全过；不改测试、不跳过、不写死；并发与重启自行验证；只做本地

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 身份认证
- type: Bugfix
- module: mfa_push_orchestrator
- typeName: MfaPushOrchestrator
