# batch48-py-safety-a-12

## 题面

组织架构调整后权限突然变宽：部门搬到新上级下面，上一级写的导出禁止立刻对整棵子树失效；子节点补策略把上级硬禁止顶掉；同一策略挂两遍，继承链越滚越长。落点是 gsb/batch48-py-safety-a-12 的 org_policy_inheritance.py，类 OrgPolicyInheritance，管策略挂载、继承求值与搬迁。

公开接口：OrgPolicyInheritance(*, max_depth=8, max_policies_per_node=8)；add_org_unit、remove_org_unit、move_subtree、attach_policy、detach_policy、effective_policy、explain、stats、snapshot、restore。拒绝抛 OrgPolicyError(code, detail)。

故障与边界：①父节点不存在 ORG_POLICY_UNKNOWN_NODE，重复 id ORG_POLICY_INVALID；②树深硬顶，超限 ORG_POLICY_DEPTH_EXCEEDED 不落树；③每节点策略数封顶，超限 ORG_POLICY_CAPACITY；④effect 非 allow/deny 或 values 非法 ORG_POLICY_INVALID；⑤子节点继承祖先，后写覆盖先写，同节点 deny 压过 allow；⑥祖先 deny 的字段是硬禁止，后代覆盖抛 ORG_POLICY_CONFLICT；⑦搬迁后目标祖先的硬禁止立即作用于整棵子树；⑧目标是自身后代 ORG_POLICY_CYCLE 且路径不变；⑨搬迁后最深节点超 max_depth 整体拒绝；⑩重复挂同一策略幂等，不增继承链；⑪detach 后回落祖先值，重复 detach 幂等。

硬约束：并发挂载与求值只看完整旧树或新树；搬迁即时生效；深度与每节点策略数硬顶；重启后快照恢复树、策略与计数，非法快照 ORG_POLICY_SNAPSHOT_INVALID 不改原状态。stats 返回 attached、detached、moved、rejected。

验收：`python3 -m pytest -q tests` 15 条全过；不改测试、不跳过、不写死；并发与重启自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 身份认证
- type: Bugfix
- difficulty: 困难
- module: org_policy_inheritance
- typeName: OrgPolicyInheritance
