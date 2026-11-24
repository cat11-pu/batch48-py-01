# batch48-py-safety-a-07

## 题面

撤销后仍有新令牌签发，未带租约的请求放行；二次撤销重计时，时钟回拨延长。范围 gsb/batch48-py-safety-a-07 的 revocation_drain_barrier.py，类 RevocationDrainBarrier，抛 RevocationError(code, detail)。

接口：configure_tenant(tenant, drain_window)、begin_revocation(tenant, now)、issue_token(tenant, subject, now)、acquire_lease(tenant, subject, request_id, now)、release_lease(tenant, lease_id)、drain_status(tenant, now)；lease_ttl 默认 60、hard_cap 600。

故障：①configure_tenant 拒非正窗口，hard_cap 封顶；②撤销后 issue_token 抛 REVOCATION_ACTIVE；③撤销后 acquire_lease 抛 REVOCATION_DRAINING，租约含 request_id、generation、expires_at；④drain_status 含 state、in_flight、drain_deadline、remaining；⑤release 首次 True、重复 False，未知抛 REVOCATION_UNKNOWN_LEASE；⑥enforce 关到期或过期租约并计 closed，窗口结束转 enforced，重复返回 0，token_state 失效；⑦snapshot 含 tenants、leases、stats；⑧排空期幂等重试可完成但不得新租约；⑨二次撤销只进一次世代，旧租约不重计时；⑩时钟回拨不延长排空；stats 为 issued/leased/released/enforced/rejected。

硬约束：并发申请与释放计数须等于调用次数；快照恢复后世代与租约可用，非法结构抛 REVOCATION_SNAPSHOT_INVALID 且状态不变。

验收：python3 -m pytest -q tests 15 条全过；不改测试、不跳过、不写死；并发与重启自验；只本地。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 身份认证
- type: Bugfix
- module: revocation_drain_barrier
- typeName: RevocationDrainBarrier
