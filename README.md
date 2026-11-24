# batch48-py-safety-c-10

## 题面

证书透明度日志接进来做资产盘点后，风控组连续退了三版报表：同一张证书在两个日志里各记一条，盘点数量翻倍；撤销名单里明明有记录，线上证书却一直显示有效，排查发现匹配用的是序列号不是指纹；已经过期的撤销记录还在发挥作用，把续签后的新证书也判成黑名单；增量拉取时游标被写小，同一批日志被重复处理三遍，下游任务队列直接翻倍；日志方偶发乱序推送，时间戳靠前的条目直接被丢掉；八路并发拉取同一条日志，最后入库的条目比实际少了一截，谁也复现不出来；报表里还有几条撤销记录的时间早于证书签发时间，却被当成有效撤销直接下架了业务证书。范围是 gsb/batch48-py-safety-c-10 的 ct_log_matcher.py，类 CtLogMatcher，负责归并日志条目、按指纹匹配撤销、维护单调游标并支持快照恢复。

公开接口：CtLogMatcher(*, max_entries=100000)；ingest(entries) -> int；dedup() -> int；match_revocation(cert, reason, at) -> dict；cursor() -> int；advance_cursor(value) -> int；processed() -> int；lookup(fingerprint) -> dict；entries() -> list[dict]；stats() -> dict；snapshot() -> dict；restore(state) -> None。拒绝统一抛 CtError(code, detail)。

故障与边界：①条目结构为 log、fingerprint、serial、not_before、not_after、timestamp，指纹必须是 64 位十六进制并统一小写，缺字段、类型不符或 not_before 晚于 not_after 抛 CT_INVALID_ENTRY，整批不落库；②ingest 返回新增唯一指纹数，processed 返回当前唯一指纹数；③同一证书在多个日志出现时只保留最早时间戳条目，但撤销匹配要覆盖所有日志来源；④dedup 折叠重复记录并返回删除条数，重复调用返回 0，lookup 的 sources 按日志名升序给出全部来源；⑤游标取已见时间戳的最大整数，advance_cursor 只接受严格更大的整数，回退或持平抛 CT_CURSOR_REGRESSION，非整数抛 CT_INVALID_CURSOR；⑥乱序时间戳的条目照常入库，游标不得回退，entries 仍按时间戳升序返回；⑦match_revocation 以指纹匹配，不看序列号，落在证书有效期内返回 revoked，并带 fingerprint、reason、at，cert 结构或 reason 非法抛 CT_INVALID_REVOCATION；⑧撤销时间超出证书有效期返回 expired 并计入 ignored，不增加撤销计数，恰好等于 not_after 仍算有效；⑨撤销时间早于证书签发时间时必须判为无效撤销，不能影响证书状态；⑩未知指纹返回 unknown，lookup 未知指纹抛 CT_UNKNOWN_CERT，lookup 返回深拷贝，改动返回值的 sources 不得影响内部记录；⑪唯一指纹数硬顶 max_entries，新增指纹超限抛 CT_CAPACITY_EXCEEDED 且整批不落库，重复指纹不受影响；⑫快照恢复后唯一集合、游标、撤销与忽略计数一致，结构非法抛 CT_SNAPSHOT_INVALID 且原状态不动。

硬约束：八路并发 ingest 不得丢条目，ingested、duplicates 与唯一集合必须与调用总量精确对应，同一指纹并发入库只能算一次新增；entries 按时间戳升序、同时间戳按指纹升序稳定返回，两次查询结果逐项相同；快照与恢复之间不共享可变结构，恢复后继续 ingest 不得与恢复前的对象互相污染。stats 固定返回 ingested、duplicates、revoked、ignored、cursor 五个整数。

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
- module: ct_log_matcher
- typeName: CtLogMatcher
