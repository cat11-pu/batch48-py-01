# batch48-py-safety-c-06

## 题面

误报抑制库上线两周，审计组的单子反而更多了：开发把误报所在的文件改个名，同一个问题每周按新问题重新提一遍；同一处误报登记过两个不同作用域的抑制，其中一个先到期，另一条也跟着失效；抑制到期后不再上报，也查不到这条被抑制过几次；两次对同一批扫描结果做过滤，返回的被抑制清单长度对不上；有人提交了一条超长原因，旧条目的到期时间被覆盖，审计记录全丢；还有人直接改接口返回的对象，抑制表跟着被改坏，运维只能临时把整条规则关掉，真正的问题也被一起盖住。范围是 gsb/batch48-py-safety-c-06 的 false_positive_suppressor.py，类 FalsePositiveSuppressor，按根因指纹管理抑制条目、过滤扫描结果并给出与基线的差异。

公开接口：FalsePositiveSuppressor(*, max_entries=50000, max_reason_len=256)；fingerprint(finding) -> str；suppress(fingerprint, reason, until, scope="file") -> dict；filter(findings, at) -> (kept, suppressed)；expire(at) -> list[str]；diff(baseline, current, at) -> dict；entries() -> list[dict]；history(fingerprint) -> dict；is_suppressed(fingerprint, at) -> bool；snapshot() -> dict；restore(state) -> None；stats() -> dict；reset_stats() -> None。拒绝统一抛 SuppressorError(code, detail)。

故障与边界：①finding 由 rule_id、path、line、context 组成，context 必须恰好 5 行，缺字段、类型不符或行数不对抛 SUPPRESS_FINDING_INVALID；②指纹只取规则 ID 与逐行归一化后的 5 行内容，逐行去首尾空白并把连续空白折叠成一个空格，同一 finding 两次计算结果一致；③文件改名只改 path，同一处误报仍要命中原来的抑制条目，不得因为路径变化产生新指纹；④同一指纹同一作用域重复登记，until 取更晚者并累计次数，更早的 until 不得把时间往回拨；⑤单条原因超过 max_reason_len 抛 SUPPRESS_REASON_TOO_LONG，旧条目原样保留；⑥条目总数硬顶 max_entries，超限抛 SUPPRESS_CAPACITY_EXCEEDED，不淘汰旧条目；⑦filter 返回保留项与被抑制项的成对结果，顺序与入参一致，at 恰好等于 until 时按未抑制处理，同一处 finding 在两份结果里都只能出现一次；⑧expire 删除到期条目并返回升序重开指纹，重复调用返回空，重开后的指纹不再命中 filter 直到重新登记；⑨diff 给出 added、removed、changed 三组，changed 记录同一 path 与 line 上指纹从 from 变到 to，同一处出现多条 finding 时按 (path, line) 归组，被抑制项不计入 added；⑩同一指纹被多个作用域抑制时以最宽作用域的 until 判定，其余条目保留且各自到期；⑪到期重开的指纹在下一轮 diff 中按新增处理，历史累计次数仍可查询；⑫作用域只接受 file、rule、global，history 返回累计次数、最后一次 until 与升序作用域列表，快照恢复后到期时间与次数不变，结构非法抛 SUPPRESS_SNAPSHOT_INVALID 且原状态不动。

硬约束：同一批 findings 连续两次 filter 结果必须一致，且不得改动抑制表；filter 只读，任何一次调用都不得新增、删除或改写条目；八路并发 filter 与 suppress 不得读到半更新的抑制表，活动条目数与累计次数必须与调用次数完全相等；快照与恢复之间不共享可变结构。stats 固定返回 active_entries、suppressed_total、expired_total、filter_calls 四个整数，reset_stats 只把后三个计数归零，stats 与 entries 都返回深拷贝，调用方改动返回值不得影响内部状态。

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
- module: false_positive_suppressor
- typeName: FalsePositiveSuppressor
