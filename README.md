# batch48-py-safety-c-01

## 题面

扫描器上线后接入客户自维护的检测规则，运维发现同一份代码在两台机器上命中的规则数量对不上：一部分机器按规则字典的遍历顺序求值，数值区间与通配混用时短路方向也不一致，同一次命中在审计台账里还被记了两遍。客户每季度整体下发规则，要求任意载入顺序下命中结果与审计数字一致。范围是 gsb/batch48-py-safety-c-01 下的 detection_rule_engine.py，类 DetectionRuleEngine，负责把规则文本编译成谓词树、对记录求值，并输出可核对的命中与结构。

规则文本单行书写：RULE <id> PRIORITY <整数> WHEN <谓词>。谓词支持 AND(...)、OR(...)、NOT(...) 以及 field OP value 形式，OP 取 ==、!=、>、>=、<、<=、~；~ 表示区分大小写的通配，* 匹配任意串、? 匹配单个字符；字符串放在双引号里，整数可带负号。公开接口包括 DetectionRuleEngine(*, fields=(), default_priority=100, max_rules=4096, max_depth=32)、register_field(name)、known_fields()、add_rule(rule_text)、remove_rule(rule_id)、reload(rule_texts)、version()、rule_ids()、evaluate(record)、explain(rule_id)、compile_errors()、hit_counts() 与 stats()；所有拒绝统一抛 DetectionError(code, detail)。known_fields() 与 rule_ids() 按字典序返回；每次接受的增删或换版使版本号加一，删除不存在的规则返回 False 且版本不变；compile_errors 只保留最近一次编译的拒绝条目，成功编译后清空；stats 的 evaluations 统计成功的求值次数。add_rule 成功返回 accepted、rule_id、priority、depth，失败返回 code 与 detail，detail 说明原因并含相关字段名或原文片段；reload 返回 previous_version、version、accepted、rejected；explain 返回 rule_id、priority、depth、tree，查询不存在的规则抛 rule.unknown；dedup_key 形如 record_id:rule_id@version，编译错误的 index 是该规则在本次批次中的位置，从 0 开始。

记录来自不同扫描任务的产物清单，字段类型并不固定，缺失与类型不符很常见。故障与边界：一、非法语法、未知字段、嵌套超限与重复 id 只登记到编译错误清单，同批其余规则照常参与求值，不能因为一条坏规则中断整批；二、编译错误条目含 index、code、detail、text，code 取 rule.syntax、rule.unknown_field、rule.depth_exceeded、rule.duplicate_id、rule.limit；三、命中按 priority 数值升序排列，同优先级按 rule_id 字典序，命中条目含 rule_id、version、priority、dedup_key 与 fields，其中 fields 要列出规则引用的全部字段，包含被短路跳过的分支所引用的字段；四、record 缺少非空 record_id 时抛 record.invalid，求值过程不得改动记录本身，同一记录重复投递时结果也不能漂移；五、大小比较只对数值成立，字符串或缺失字段一律不命中，== 与 != 按原值比较；六、hit_counts 的键是 rule_id@version，同一 record_id 在同一版本下重复求值只累加一次；七、reload 整体替换规则集并原子生效，返回旧版本号以及本次接受和拒绝的条数，重复载入与当前完全相同的规则集不产生新版本，旧命中计数继续沿用；八、规则集上限 4096 条、单规则嵌套 32 层，超限时不得替换已有规则；九、explain 输出的 tree 由 op 为 and、or、not、cmp 的节点组成，cmp 节点含 field、cmp、value，深度按最长路径计算；十、stats 返回 version、rules、evaluations、hits，其中 hits 统计命中条目总数而非去重后的计数；十一、同一规则集两次编译得到的谓词树与命中顺序必须逐项一致，不得依赖字典遍历顺序；十二、并发 evaluate 与 reload 只能看到完整旧版本或完整新版本，不能看到半套规则。

硬约束：evaluate 必须无副作用并全程持锁，版本切换与计数更新必须原子，求值只读当前版本快照，十路并发下不得丢计数或重复累加。验收：python3 -m pytest -q tests 的 15 条测试全部通过；不得修改测试、跳过或按固定输入写死；并发场景自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 漏洞检测与攻防
- type: Bugfix
- difficulty: 地狱
- module: detection_rule_engine
- typeName: DetectionRuleEngine
