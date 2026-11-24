# batch48-py-safety-a-11

## 题面

外部身份同步连出单子：同一人被两个组命中后多出未授予角色；离职员工移出目录组，那组给的审批权还留着；映射停用再启用，已退出成员又被赋回角色。落点是 gsb/batch48-py-safety-a-11 的 group_mapping_registry.py，类 GroupMappingRegistry，管组到租户角色映射与派生。

公开接口：GroupMappingRegistry(*, max_mappings_per_tenant=200)；add_mapping、remove_mapping、disable_mapping、enable_mapping、resolve_roles、sync_membership、grant_manual、roles_of、drift_report、reconcile、stats、snapshot、restore。拒绝抛 GroupMappingError(code, detail)。

故障与边界：①映射缺字段或 priority 非整数 GROUP_MAPPING_INVALID，重复 id GROUP_MAPPING_CONFLICT；②每租户条数封顶，超限 GROUP_MAPPING_CAPACITY_EXCEEDED 不落表；③同组多映射按优先级、模式长度、id 裁决；④显式优先于通配，不叠加；⑤多组取并集，未命中记 null；⑥成员移出只回收该组派生角色，手工授予保留；⑦重复同步幂等，计数不涨；⑧remove_mapping 与 disable_mapping 回收派生角色，未知 id GROUP_MAPPING_UNKNOWN；⑨重新启用只派生仍有效成员，停用期过期成员不恢复；⑩drift_report 报差异，reconcile 修复并计数；⑪并发同步同组只应用一次。

硬约束：同组同步串行；重复事件幂等；每租户条数硬顶；重启后快照恢复映射、成员、派生角色与计数且不共享结构；非法快照 GROUP_MAPPING_SNAPSHOT_INVALID 原状态不变。stats 返回 grants、revocations、syncs、rejected。

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
- module: group_mapping_registry
- typeName: GroupMappingRegistry
