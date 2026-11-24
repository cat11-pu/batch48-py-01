# batch48-py-safety-b-12

## 题面

演练周的破窗通道出了事故：值班人没填事件编号就把高权操作放了出去；复核人和操作者是同一个账号；动作窗口早该到期，脚本还能继续删资源；同一个事件被反复激活，台账里堆出三份互不相干的授权；真出问题时，没人能说清到底执行过哪些动作；授权关掉之后旧凭据又被捡起来用；月度清理把已关闭记录连同动作流水一起扫掉；拿着集群级授权去改另一个命名空间的资源也没人拦。范围是 gsb/batch48-py-safety-b-12 的 breakglass_grant_ledger.py，类 BreakglassLedger，负责破窗授权的启用、复核、动作流水、关闭与清理，所有拒绝抛 BreakglassError(code, detail)。

公开接口：BreakglassLedger(*, default_ttl=600, max_ttl=3600, closed_retention=86400, clock=None)；activate(operator, scope, incident_id, ttl=None, now=None) -> dict；acknowledge(grant_id, reviewer, now=None) -> dict；use(grant_id, action, scope=None, now=None) -> dict；close(grant_id, note, now=None) -> dict；sweep(now) -> int；audit_trail(grant_id) -> dict；active() -> list；get(grant_id) -> dict；snapshot() -> dict；restore(state) -> None；stats() -> dict；reset_stats() -> None。scope 形如 cluster:prod 或 cluster:prod/ns:payments，每段是两截用冒号隔开的标识，段与段用斜杠分隔。

编号从 1 起，授权形如 bg-1。activate 返回 grant_id、operator、scope、incident_id、activated_at、expires_at、ttl、acknowledged、reviewer、note 与 state，note 在关闭前为 None；acknowledge 返回 acknowledged、reviewer 与 acknowledged_at；use 返回 grant_id、seq、action、scope、reviewed 与 audit_size，seq 从 1 起按执行顺序递增；close 返回 grant_id、state、closed_at 与 note；audit_trail 返回 grant_id、incident_id、operator、state、entries、closed_at、note、acknowledged 与 reviewer，entries 每项含 seq、at、action、scope；get 按标识返回授权记录，未知标识抛 BREAKGLASS_GRANT_UNKNOWN；stats 固定返回 activations、acknowledgements、uses、closes、swept、unreviewed_uses。

故障与边界：一、事件编号去空白后为空、操作者为空、scope 不合法、ttl 非正或超过 max_ttl 一律拒绝；二、复核人不得为空且不得等于操作者；三、复核发生在授权到期之后或已关闭之后都要拒绝，同一授权只能复核一次；四、动作必须在到期前执行，now 等于 expires_at 即拒绝；五、已关闭授权不得再执行动作，关闭必须带非空说明，重复关闭幂等；六、动作的 scope 只能等于授权 scope 或是其下级路径，越界拒绝；七、同一事件已有他人激活的活跃授权时，新的激活按事件占用拒绝；八、同一事件在多路并发激活下只能存在一份活跃授权；九、sweep 把到期授权置为过期、把超过保留期的已关闭记录清出台账并返回处理条数；十、动作流水与授权记录分开保存，授权记录被清理后流水仍要能按授权标识追溯；十一、同一事件再次激活应复用原授权并延长时限，而不是另建一份；十二、未复核期间执行的动作要单独计数，复核后执行的动作不得再计入该计数。

快照覆盖授权台账、动作流水与统计，内容为深拷贝；恢复后已过期的授权立即失效、流水条数与顺序与恢复前一致，结构非法抛 BREAKGLASS_SNAPSHOT_INVALID 且原状态不变。硬约束：activate、acknowledge、use、close、sweep 全程互斥，并发激活与清理交错时同一事件不得留下两份活跃授权；复核状态、到期时刻与流水序号在快照恢复后必须连续，不能回退或重号；台账与流水均受保留期约束，不得无界增长。

拒绝码取 BREAKGLASS_OPERATOR_INVALID、BREAKGLASS_INCIDENT_REQUIRED、BREAKGLASS_SCOPE_INVALID、BREAKGLASS_TTL_INVALID、BREAKGLASS_SELF_REVIEW、BREAKGLASS_REVIEWER_REQUIRED、BREAKGLASS_ALREADY_REVIEWED、BREAKGLASS_GRANT_UNKNOWN、BREAKGLASS_GRANT_EXPIRED、BREAKGLASS_GRANT_CLOSED、BREAKGLASS_ACTION_INVALID、BREAKGLASS_SCOPE_DENIED、BREAKGLASS_INCIDENT_BUSY、BREAKGLASS_NOTE_INVALID、BREAKGLASS_SNAPSHOT_INVALID。验收：python3 -m pytest -q tests 的 15 条全部通过，不得修改测试、跳过或按固定输入写死；并发激活与重启恢复自行验证；只做本地实现，不涉及上游提交。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 系统安全
- type: Bugfix
- difficulty: 地狱
- module: breakglass_grant_ledger
- typeName: BreakglassLedger
