# batch48-py-safety-c-14

## 题面

全量扫描改成增量以后，值班同学发现改了一个被所有人引用的公共库，计划里只有它自己，下游十几个服务一次都没重扫；补上闭包之后，两次变更之间又开始反复全量重跑，扫完的批次还挂在待办里；有人把一份文件原样保存一次，摘要没变，整条依赖链却被重新排了一遍；预算小的机器上计划被截断，剩余文件提交后直接消失，第二天只能靠人工补扫。范围是 gsb/batch48-py-safety-c-14 的 incremental_scan_planner.py，类 IncrementalScanPlanner，依据文件摘要与依赖图求最小重扫集合，并按预算分批提交。

公开接口：IncrementalScanPlanner(*, max_batch=10000, max_edges=500000)；update(path, digest) -> bool；remove(path) -> bool；add_dependency(referrer, dependency) -> tuple；plan(budget) -> dict；commit(plan_id) -> dict；pending() -> list[str]；explain(path) -> dict；files() -> list[dict]；dependencies(path) -> list[str]；dependents(path) -> list[str]；snapshot() -> dict；restore(state) -> None；stats() -> dict；reset_stats() -> None。拒绝统一抛 PlannerError(code, detail)。

依赖方向按引用方到被引用方建边，变更文件的反向闭包就是必须一起重扫的集合。plan 返回 plan_id、files、total、truncated 四个字段，files 按路径升序去重；commit 返回 plan_id、committed、remaining。

故障与边界：

1. update 的路径为空、摘要不是非空字符串分别抛 PLAN_INVALID_PATH 与 PLAN_INVALID_DIGEST，本次调用不改任何状态。
2. 摘要与上次相同返回 False，不产生待扫项，也不触发下游闭包；摘要变化才返回 True。
3. remove 返回文件是否存在，重复删除返回 False；被删除的文件本身不进计划，但它的下游引用方必须进计划。
4. add_dependency 对同一对引用关系幂等，返回二元组；端点尚未登记时自动建立占位记录，不存在的路径不得报未知错误。
5. 依赖边总数上限为 max_edges，超限抛 PLAN_EDGE_CAPACITY_EXCEEDED，整条边不写入。
6. 待扫集合是变更文件加反向闭包，环依赖整体加入且每个文件只出现一次。
7. plan 在没有待扫文件时抛 PLAN_NOTHING_TO_SCAN；单批预算上限为 max_batch，预算先于待扫集合校验。
8. 预算非正整数抛 PLAN_BUDGET_INVALID，超过 max_batch 抛 PLAN_BUDGET_EXCEEDED，两种情况都不产生计划。
9. 计划 ID 从 1 开始单调递增；commit 未知 ID 抛 PLAN_UNKNOWN，重复提交同一 ID 抛 PLAN_ALREADY_COMMITTED。
10. commit 只完成该计划内的文件，返回 committed 与 remaining；预算截断后的剩余文件必须在下一批继续给出，不能因提交而丢失。
11. explain 返回 path、pending、reason、roots，reason 取 changed、dependent、clean、deleted 之一，roots 是导致待扫的变更文件升序列表。
12. 快照带版本号，恢复后待扫集合、依赖边与已提交批次一致；版本不符或结构缺失抛 PLAN_SNAPSHOT_INVALID，原状态不动。

硬约束：并发 update 与 plan 只能看到一致快照，读方不得看到半条依赖边或半个待扫项；同一状态连续两次 plan 的文件集合与顺序一致，只有计划 ID 递增；pending 与 files 排序稳定且返回深拷贝；预算与边数是硬顶，拒绝时不留部分计划或部分边。

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
- module: incremental_scan_planner
- typeName: IncrementalScanPlanner
