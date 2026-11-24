# batch48-py-safety-b-15

## 题面

沙箱平台上线权限对账后，安全组收到一批互相打架的结论：声明里写的是 FS.Read，实际记录却按大小写分成两份；作业用了声明之外的 proc.exec，报告仍然显示合规；统计只看次数，第一回出现的时间永远是空的；声明过期两天，观察接口照样接收新计数；同一个权限用短名和全名各报一次，次数直接翻倍；基线在声明之后被后续观察改写，复盘时对不上号；真正该报的过度授予反而没出现在报告里。范围是 gsb/batch48-py-safety-b-15 的 sandbox_permission_auditor.py，类 PermissionAuditor，负责权限声明归一、观察计数、偏差报告、过期处理与基线快照，所有拒绝抛 PermissionAuditorError(code, detail)。

公开接口：PermissionAuditor(*, max_jobs=32, max_permissions=64, aliases=None, clock=None)；declare(job_id, permissions, ttl=None, now=None) -> dict；observe(job_id, permission, count=1, now=None) -> dict；report(job_id) -> dict；expire(now) -> int；baseline(job_id) -> dict；get(job_id) -> dict；jobs() -> list；snapshot() -> dict；restore(state) -> None；stats() -> dict；reset_stats() -> None。

权限名统一小写，段与段用点分隔，至少两段；内置别名把 read、write、open、connect、listen、dns、exec、spawn 分别归到 fs.read、fs.write、fs.open、net.connect、net.listen、net.dns、proc.exec、proc.spawn，别名表可由构造参数覆盖。declare 返回 job_id、declared、version、declared_at、ttl 与 expires_at，声明的权限去重后按字典序排列，ttl 省略表示不过期；observe 返回 job_id、permission、count、total、first_seen_at 与 declared，total 为该权限累计次数；report 返回 job_id、declared、observed、undeclared、unused、violations、first_seen、total_observations、compliant、version 与 baseline_version，violations 每项含 permission 与 count；baseline 返回 job_id、version、counts 与 captured_at；get 返回声明与运行态，jobs 返回已登记任务标识；stats 固定返回 jobs、observations、undeclared_hits、reports、expired_jobs。

故障与边界：一、声明与观察都要先做大小写归一和别名归一，同一逻辑权限只保留一条；二、声明为空、名字不合法、ttl 非正、任务重复或任务数超限一律拒绝；三、观察计数必须是正整数，任务未知、名字不合法都拒绝；四、观察到未声明的权限不得当成通过，要按未声明使用计入偏差，报告整体判为不合规；五、首次出现时刻取该权限最早一次观察的时刻，不能只累加次数；六、声明到期后观察、报告与基线都拒绝，expire 只回收到期声明并返回条数；七、同一权限的别名必须先归一再加总，否则一次使用会被重复计费；八、声明了但整个生命周期从未使用的权限属于过度授予，应与未声明却实际使用分开报告，且不得计入违规；九、基线在声明时固化，后续观察不得改写，重复读取返回同一份内容；十、报告中的观察表按归一后的名字聚合，未声明列表按字典序；十一、快照覆盖声明、计数、首次出现时刻、基线版本与统计，内容为深拷贝；十二、恢复后的偏差报告与恢复前逐项一致，结构非法抛 PERMISSION_SNAPSHOT_INVALID 且原状态不变。

硬约束：observe 与 report 全程持锁，八路并发对同一权限各记两百次时总数必须精确等于一千六百，不得丢计数；基线只读，声明与计数在快照恢复后不得共享可变结构；任务数与单任务声明条目都是硬顶。

拒绝码取 PERMISSION_JOB_INVALID、PERMISSION_JOB_DUPLICATE、PERMISSION_JOB_LIMIT、PERMISSION_DECLARE_INVALID、PERMISSION_DECLARE_LIMIT、PERMISSION_NAME_INVALID、PERMISSION_TTL_INVALID、PERMISSION_COUNT_INVALID、PERMISSION_UNKNOWN_JOB、PERMISSION_DECLARATION_EXPIRED、PERMISSION_SNAPSHOT_INVALID。验收：python3 -m pytest -q tests 的 15 条全部通过，不得修改测试、跳过或按固定输入写死；别名归一与并发计数自行验证；只做本地实现，不涉及上游提交。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 系统安全
- type: Bugfix
- difficulty: 地狱
- module: sandbox_permission_auditor
- typeName: PermissionAuditor
