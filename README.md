# batch48-py-safety-b-09

## 题面

合规审计要求把操作日志做成可验证的哈希链，第一批数据交上去就被退回：某批封存段的摘要没有带上前一段的摘要，删掉中间一整段后校验照样通过；有人把某条事件的正文换成另一份等长的内容，条数和总长度都没变，复核工具报全绿；并发写入时两个协程拿到同一个序号，后写的把前一条覆盖掉；已经封存的段还能被重新封存，内容被替换也没人发现；检查点可以被反复重放，导入外部段时摘要对不上也照单全收；带 +08:00 偏移的时间戳混进了日志，跨时区复算时顺序全乱；导出接口把半截段也打包出去，接收方拼不回完整链；检查点比对只比条数，永远发现不了内容漂移。范围是 gsb/batch48-py-safety-b-09 的 audit_seal_chain.py，类 AuditSealChain，负责追加审计事件、按批封存为链段、校验摘要、生成检查点并支持导入导出。

公开接口：AuditSealChain(*, segment_size=3, max_records=1000, digest_len=16, clock=None)；append_event(event) -> int；seal(up_to=None) -> dict；verify(segment) -> dict；checkpoint() -> dict；adopt_checkpoint(checkpoint) -> dict；find_gap(start, end) -> list；export(start, end) -> dict；import_payload(payload) -> dict；head() -> dict；snapshot() -> dict；restore(state) -> None；stats() -> dict；reset_stats() -> None。拒绝统一抛 SealChainError(code, detail)。

返回结构与入参：事件含 actor、action、target 与 at；append_event 返回序号，段号与检查点编号从 1 起；seal 不带参数时封存到最新序号，返回 segment_id、first_seq、last_seq、count、prev_digest、digest 与 events；verify 返回 ok、segment_id、count 与 digest；checkpoint 返回 checkpoint_id、last_seq、last_digest、segments 与 issued_at；adopt_checkpoint 返回 adopted 与 last_seq；find_gap 按已封存段的覆盖计算缺口区间；export 返回 version、start、end 与 segments；import_payload 返回 imported、last_seq 与 last_digest；head 返回 last_seq、last_sealed、segments、pending 与 last_digest；digest 取 digest_len 位十六进制；snapshot 返回 version、last_seq、last_digest、segments、checkpoint 与 stats；stats 返回 appended、sealed（段数）、verified 与 imported。

故障与边界：一、段摘要必须吃进上一条摘要，第一条的链接值是空串，漏掉就断链；二、并发追加不得拿到相同序号，序号从 1 连续递增；三、已封存区间不可变，重新封存或把新事件塞回旧区间都要拒绝；四、校验要重算摘要，不能只比条数与长度；事件正文被等长替换时，必须靠逐段摘要比对发现；五、检查点只能采纳一次且只能向前，旧锚点重放要拒绝，指向未来的锚点同样拒绝；六、导入时摘要不符、序号不连续都要拒绝，不得先落一半再报错；七、事件时间统一按 UTC 校验，带非零偏移或无法解析的时间戳拒绝；八、导出只覆盖完整封存段，半截重叠的范围拒绝；九、封存范围受每段条数上限约束，超限拒绝而不是截断；十、缺口按已封存段的覆盖区间报出，中间段被整段删除时要给出缺失的区间而不是只说总条数不符；十一、快照保存最后序号、最后摘要、已封存段与检查点，重启后链保持连续；十二、记录总数、段大小与摘要长度都是硬顶。

硬约束：append_event 与 seal 交错时已封存段不可变，六路并发追加后序号集合必须恰好是 1 到总数；快照恢复后继续封存的段必须接在恢复点摘要之后，链校验逐段通过；事件总量有硬顶，超限拒绝而不是丢弃最旧。

验收：python3 -m pytest -q tests 的 15 条必须全过，不得修改测试、跳过或按固定输入写死；删段检测与并发封存自行验证；拒绝码用 SEAL_EVENT_INVALID、SEAL_TIMESTAMP_INVALID、SEAL_RECORD_LIMIT、SEAL_SEAL_TOO_LARGE、SEAL_SEAL_RANGE、SEAL_SEGMENT_INVALID、SEAL_DIGEST_MISMATCH、SEAL_CHECKPOINT_INVALID、SEAL_CHECKPOINT_STALE、SEAL_GAP_RANGE、SEAL_EXPORT_RANGE、SEAL_IMPORT_INVALID、SEAL_IMPORT_GAP、SEAL_SNAPSHOT_INVALID；只做本地实现，不涉及上游提交。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 系统安全
- type: Bugfix
- difficulty: 困难
- module: audit_seal_chain
- typeName: AuditSealChain
