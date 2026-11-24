# batch48-py-safety-c-11

## 题面

规则库涨到两万多条以后，夜间扫描窗口开始连续超时：每条规则都要对每个文件完整求值一遍，单机队列排到了第二天早上。值班同学先按扩展名粗筛，漏报立刻成片出现：只匹配文件中部关键字的规则被当成不相关扔掉，没写扩展名的规则整批跳过，审计追查时连为什么跳过都说不清。安全组连续两周收到漏报投诉，规则组和平台组互相认为问题出在对方：平台说规则没有命中，规则组说求值根本没跑到这条规则。范围是 gsb/batch48-py-safety-c-11 的 rule_candidate_index.py，类 RuleCandidateIndex，为规则建立保守的候选索引，只在确定安全时剪枝。

公开接口：RuleCandidateIndex(*, max_entries=200000, max_keywords=16)；register(rule_id, file_kinds, keywords, cost=1) -> dict；candidates(path, head_bytes, whole_file=False) -> list[str]；explain(rule_id, path, head_bytes, whole_file=False) -> dict；audit(rule_id) -> dict；remove(rule_id) -> bool；rules() -> list[dict]；snapshot() -> dict；restore(state) -> None；stats() -> dict；reset_stats() -> None。拒绝统一抛 RuleIndexError(code, detail)。

故障与边界：

1. 文件类型统一转小写并补前导点，去重后升序保存；rule_id 为空、cost 不是正整数、关键字不是字符串都拒绝，索引保持原样。
2. 单条规则关键字超过 max_keywords 抛 RULE_KEYWORDS_LIMIT；条目数达到 max_entries 后新注册抛 RULE_CAPACITY_EXCEEDED，不许淘汰旧条目。
3. 同一 rule_id 重复注册抛 RULE_DUPLICATE，旧条目与审计计数原样保留。
4. 剪枝只允许两种确定情形：文件类型明确不匹配；head_bytes 覆盖整个文件（whole_file 为真）且声明关键字全部不在样本里。
5. 头部样本之外的命中、空的头部样本、被截断的头部，都不能作为排除依据；规则未声明文件类型时对所有文件保留候选。
6. candidates 结果按 cost 升序、同 cost 按 rule_id 升序，去重，且不随注册顺序变化。stats 固定返回 rules、calls、served、pruned 四个整数，reset_stats 只清零后三个计数，规则表不受影响。
7. audit 记录命中次数、剪枝次数与最后一次剪枝依据，依据取 retained、no_kind_declared、kind_matched、keyword_matched、no_keyword_declared、kind_mismatch、keyword_miss 之一。
8. remove 返回是否真的移除；移除后该规则立即不再出现在候选里，但审计计数仍可查询。
9. explain 对单条规则给出 rule_id、candidate、basis 三个字段，判定必须和 candidates 一致。
10. 快照带版本号，版本不符或字段缺失按结构非法处理；恢复后同一批文件的候选集合与顺序完全一致，抛 SNAPSHOT_INVALID 时原状态不动。
11. 条目上限与关键字上限是硬顶，超限拒绝时不得写入半条规则。

硬约束：八路并发 register 与 candidates 不得漏掉已注册规则，读方不得看到半更新的索引；同一输入连续两次 candidates 逐项一致；rules 与快照返回深拷贝，调用方改动不影响内部状态，并发删除时索引也不得出现悬空条目。

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
- module: rule_candidate_index
- typeName: RuleCandidateIndex
