# batch48-py-safety-c-08

## 题面

SBOM 汇入安全平台后，报表彻底不可信：同一个组件在清单里有七八个写法，被当成七八个不同组件；间接依赖里的漏洞全被算到根组件头上，负责人拿着报表找不到真正要升级的那个包；版本区间判定把边界版本算反，已经修复的版本还在告警；开发依赖里的漏洞和运行时混在一起，值班同学半夜爬起来发现升错了包；两个包互相依赖时整份分析卡死不出结果；同一张图刷新两次，受影响的组件顺序都不一样，报表没法比对。范围是 gsb/batch48-py-safety-c-08 的 sbom_dependency_graph.py，类 SbomDependencyGraph，负责 purl 归一化、按作用域维护依赖图并按公告区间给出受影响组件与最短路径。

公开接口：SbomDependencyGraph(*, max_nodes=100000, max_edges=500000)；normalize_purl(purl) -> str；add_component(purl, scope="runtime") -> str；add_edge(parent, child, scope="runtime") -> tuple；affected(advisory) -> list[dict]；paths_to_root(component) -> list[list[str]]；explain(component) -> list[str]；parse_version(version) -> tuple；version_in_range(version, constraint) -> bool；roots() -> list[str]；components() -> list[str]；edges() -> list[tuple]；stats() -> dict；snapshot() -> dict；restore(state) -> None。拒绝统一抛 SbomError(code, detail)。

故障与边界：①purl 归一化把类型、命名空间段与名称转小写，剔除空命名空间段，版本各数字段去前导零，限定参数键转小写并按序排列，子路径不参与身份，非法写法抛 SBOM_INVALID_PURL；②add_component 把同一组件的多种写法折叠成一个节点并保留原始标识，explain 返回升序原始串，stats 的 originals 统计折叠数量，同一版本的不同写法不得产生两个节点；③add_edge 对同一 (parent, child) 同一作用域幂等，不同作用域保留成两条边，端点缺失时自动补建；④节点上限 max_nodes、边上限 max_edges 超限分别抛 SBOM_NODE_LIMIT 与 SBOM_EDGE_LIMIT 且不改图，作用域只接受 runtime 与 dev，其他值抛 SBOM_INVALID_SCOPE；⑤加边成环抛 SBOM_CYCLE_DETECTED，detail 给出环上的 purl 列表，图保持原样；⑥parse_version 去掉数字段前导零后返回 (major, minor, patch, prerelease) 四元组，非法版本抛 SBOM_INVALID_VERSION；⑦version_in_range 支持 >=、>、<=、<、=、形如 3.1.* 的通配与 *，端点包含性严格按运算符，任何一段缺数字都算非法区间；⑧区间串非法抛 SBOM_INVALID_CONSTRAINT；⑨affected 只沿运行时分发，返回受影响组件、版本与从根到该组件的最短依赖路径，结果按 purl 升序，开发作用域不参与传播但保留在图中；⑩预发布版本按语义化版本规则排序，候选版本低于同号正式版且落在开放区间内；⑪同一组件以开发与运行时两种作用域被引用时按运行时传播并保留两条边；⑫区间内某个版本在图中缺失时不影响其余组件的判定，也不报错；快照恢复后节点、边、原始标识与统计一致，非法结构抛 SBOM_SNAPSHOT_INVALID 且原状态不动。

硬约束：同一张图连续两次 affected 的结果与顺序必须一致，paths_to_root 去重后按路径字典序返回，roots 按 purl 排序；并发只读查询与增量加边安全，读方不得看到半条边，超限拒绝时整次调用不写入任何节点或边；节点与边上限为硬顶，不接受先写后裁。

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
- module: sbom_dependency_graph
- typeName: SbomDependencyGraph
