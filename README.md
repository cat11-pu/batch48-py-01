# batch48-py-safety-a-13

## 题面

结账前审批线出问题：主管把审批权转给助理、助理再转给实习生，主管被撤权后实习生仍能批；同一单子被本人和代班各点一次，票数算了两票。落点是 gsb/batch48-py-safety-a-13 的 approval_delegation_chain.py，类 ApprovalDelegationChain，管代班链与到期回收。

公开接口：ApprovalDelegationChain(*, max_depth=3, max_duration=86400)；delegate_approval(delegator, delegate, scopes, ttl)、accept_delegation、effective_approvers、create_request、decide、revoke_delegation、expire_due、snapshot、restore。拒绝抛 DelegationError(code, detail)。

故障与边界：①请求人不得在审批人集合里，quorum 越界抛 DELEGATION_SELF_APPROVAL、DELEGATION_INVALID_REQUEST；②代班须被接受才生效，未接受前 effective_approvers 只有本人；③时长与链深硬顶，超出抛 DELEGATION_DURATION_EXCEEDED、DELEGATION_DEPTH_EXCEEDED；④转授范围只能收窄，扩大抛 DELEGATION_SCOPE_EXCEEDED；⑤链上任何人不得审批自己发起的请求；⑥同一请求同一审批人重复投票只计一票；⑦代班人转授后失权，下游代班同步失效；⑧撤销任一环级联下游，重复代班与撤销幂等；⑨到期后未决请求里代班的票撤回，回原审批人队列；⑩到期后已打开的审批页提交被拒。

硬约束：投票串行计数，并发每人只计一票；撤销与到期级联一步完成；链深与时长硬顶；重启后快照恢复请求、票与代班，非法快照 DELEGATION_SNAPSHOT_INVALID 不改原状态。stats 返回 delegated、accepted、revoked、decided、rejected。

验收：`python3 -m pytest -q tests` 15 条全过；不改测试、不跳过、不写死；并发与重启自验；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 身份认证
- type: Bugfix
- difficulty: 困难
- module: approval_delegation_chain
- typeName: ApprovalDelegationChain
