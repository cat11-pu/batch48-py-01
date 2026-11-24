# batch48-py-safety-c-13

## 题面

一次扫描出了三千多条告警，同一条污点路径因为入口不同被拆成几十条，分诊同学一天只能处理两百条。有人把两簇手工并成一条，第二天重跑又散开；想从合并后的簇里挑几条单独跟进，拆完下一轮又被自动并回去；告警一多，列表被悄悄截断，谁也说不清少了哪些；统计报表上每条规则的簇数每次刷新都不一样，交接班没法对账。范围是 gsb/batch48-py-safety-c-13 的 finding_clusterer.py，类 FindingClusterer，负责按根因键聚类、手工合并与拆分，并保留可追溯的痕迹。

公开接口：FindingClusterer(*, max_findings=200000, max_members=4096)；add(finding) -> str；cluster() -> list[dict]；merge(cluster_a, cluster_b) -> str；split(cluster_id, finding_ids) -> str；members(cluster_id) -> list[str]；explain(cluster_id) -> dict；findings() -> list[dict]；stats() -> dict；snapshot() -> dict；restore(state) -> None；reset_stats() -> None。拒绝统一抛 ClusterError(code, detail)。

根因键由规则 ID、汇聚点签名与来源类型组成，不含行号与调用入口；簇的 roots 以 rule|sink|source 的形式展示。簇内成员按文件、行号、告警 ID 升序，簇按根因键升序输出。

故障与边界：

1. finding 必填 rule_id、path、line、sink、source，行号必须是正整数；缺字段或类型不符抛 FINDING_INVALID，本次不写入任何数据。
2. 告警 ID 可显式给出，重复抛 FINDING_DUPLICATE；未给出时按 f-000001 递增补号。
3. 告警总数上限为 max_findings，超限抛 FINDING_CAPACITY_EXCEEDED，不淘汰旧告警。
4. 同一根因键出现在不同文件时合并成一簇，路径差异只影响成员，不新建簇。
5. 单簇成员上限为 max_members，超出时标记 overflow 并按告警 ID 顺序截断，不按到达顺序。
6. merge 保留第一个簇的 ID，第二个 ID 作废；成员取并集后重新排序；两簇根因键不同时保留双键。
7. merge 同一簇或未知簇分别抛 MERGE_INVALID 与 CLUSTER_UNKNOWN，失败时两簇都不改动。
8. split 把指定成员移入新簇并返回新 ID；成员不属于该簇、列表为空或簇未知时抛错且原簇不动。
9. 拆分后的簇在根因键未变时保持分离，下一轮 cluster 不得把它自动并回原簇。
10. cluster 连续两次结果逐项一致；stats 给出 findings、clusters、by_rule、merges、splits，其中 by_rule 统计每条规则名下的簇数，reset_stats 只清零 merges 与 splits。
11. 快照带版本号，恢复后簇、成员与合并痕迹一致；版本不符或结构缺失抛 SNAPSHOT_INVALID，原状态不动。
12. 成员列表与快照都是深拷贝，调用方改动不回流；统计计数与簇结构分离，失败调用不污染计数。

硬约束：并发 add 与 cluster 不得出现重复成员或半条告警，读方拿到的每簇成员必须唯一；单簇成员上限是硬顶，超出只截断并标记，不报错也不丢告警总数；告警上限是硬顶，超出整次调用拒绝；告警 ID 一旦分配不得复用。

验收：python3 -m pytest -q tests 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发与快照恢复场景自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 漏洞检测与攻防
- type: Bugfix
- difficulty: 困难
- module: finding_clusterer
- typeName: FindingClusterer
