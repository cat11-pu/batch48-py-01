# batch48-py-safety-c-12

## 题面

跨文件污点分析上线后，第一次全量扫描报出的传播路径，在增量重跑时整批消失；把递归函数纳进来之后，分析进程卡在迭代里不出来，报表上既没有结果也没有超时提示；规则同学手工补的汇聚点，因为摘要按整个参数列表粗化，被算到了不相干的参数上。范围是 gsb/batch48-py-safety-c-12 的 taint_summary_fixpoint.py，类 TaintSummary，为每个函数维护参数位次级的摘要，并按调用图迭代到不动点。

公开接口：TaintSummary(*, max_functions=50000, max_callees=4096, iteration_limit=64)；index_function(name, params, callees) -> dict；summarize(name) -> dict；analyze(entry) -> dict；invalidate(names) -> list[str]；dependencies(name) -> list[str]；explain(name) -> dict；functions() -> list[str]；snapshot() -> dict；restore(state) -> None；stats() -> dict；reset_stats() -> None。拒绝统一抛 TaintError(code, detail)。

摘要记录参数位次到返回值、参数位次到汇聚点的传播，形如 0->ret、1->sink:sql，flows 按参数位次数值升序、同位次 ret 在 sink 之前排列。callees 条目支持 target、param、arg、ret 四个键，arg 是目标函数的参数位次，必须精确使用，不得把所有参数当成一个整体；ret 为假时不连接目标返回值。只有 param 与 sink 的条目表示直接汇聚，只有 param 与 ret 的条目表示参数直接成为返回值。

故障与边界：

1. name 为空、params 不是字符串或含重复项抛相应错误；param 或 arg 越界、负数、类型不符抛 TAINT_EDGE_INVALID，该函数不进入索引。
2. 单函数调用边超过 max_callees 抛 TAINT_CALLEES_LIMIT；函数总数达到 max_functions 后新函数抛 TAINT_FUNCTION_LIMIT，两者都不得写入半份定义。
3. summarize 未知函数、invalidate 未知名字都抛 TAINT_UNKNOWN_FUNCTION，已有摘要不受影响。
4. 同一函数连续两次 summarize 结果完全相同，flows 与 version 一致；返回结构是深拷贝，调用方修改不回流。
5. analyze 沿 target 求调用闭包，functions 按名字升序，flows 逐条带函数名前缀，形如 entry::0->ret。
6. invalidate 返回被重算的函数名升序列表，只重算失效函数及其依赖闭包，不碰无关分支。
7. 递归与互递归按强连通分量整体收敛，迭代上限为 64 轮；达到上限仍不收敛的摘要标记 unstable，仍要参与 analyze 并带标记输出。
8. 递归函数只经自身回边传播的路径也要计入摘要，不能因为成环被漏掉。
9. 被失效的函数如果没有任何依赖方，重算不得改变其他函数的摘要版本号。
10. 快照带版本号，恢复后同一函数的摘要与 analyze 结果逐项一致；版本不符或结构缺失抛 TAINT_SNAPSHOT_INVALID，原状态不动。

硬约束：并发 analyze 与 invalidate 只能看到完整摘要版本，不得出现半新半旧；索引定义、摘要缓存与统计计数分离，失败调用不得污染任何一层；函数与调用边上限为硬顶，超限整次调用不产生部分定义。

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
- module: taint_summary_fixpoint
- typeName: TaintSummary
