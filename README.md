# batch48-py-game-a-22

## 题面

新赛季上线后，技能释放系统接连出现问题：拿到两层充能的位移技能点一次就黑掉两层；急速叠到八成以后冷却时间反而比四成时更长，因为每次加成都往上顶；暂停菜单打开期间技能冷却继续走，回来以后有些技能冷却已经好了，另一些在暂停结束时又凭空多出一段冷却；同一个技能被连点器和宏同时触发时，扣掉的充能层数和实际效果次数对不上；切换场景回来，所有充能层数归零，但冷却显示还在倒计时。范围是 gsb/batch48-py-game-a-22 下的 cooldown_charge_tracker.py，类 CooldownTracker，负责技能登记、充能消耗与恢复、全局冷却、急速上限、暂停恢复和跨场景快照。

公开接口：CooldownTracker(*, gcd_ms: int = 1500, haste_cap: float = 0.5)；register(skill_id, *, cooldown_ms, charges=1, recovery_ms=None, gcd_ms=None) -> dict；use(skill_id, now) -> dict；ready(skill_id, now) -> bool；charges(skill_id) -> int；remaining(skill_id, now) -> int；apply_haste(pct) -> dict；pause(now) -> dict；resume(now) -> dict；advance(now) -> dict；snapshot() -> dict；restore(state) -> None；stats() -> dict；reset_stats() -> None。所有拒绝抛 CooldownError(code, detail)。

登记返回 skill、cooldown_ms、charges、recovery_ms、gcd_ms 五个字段；成功释放返回 skill、charges、recovery_at、gcd_ready_at 四个字段；apply_haste 返回 haste 与 cap，pause 返回 paused_at，resume 返回 resumed_at，advance 返回 recovered 映射，值为本次恢复的层数，没有恢复时为空映射。

故障与边界：一、单次 use 只消耗一层充能，没有充能时抛 CD_NO_CHARGES；二、急速是累加值并封顶 haste_cap，反复调用不得超过上限，超出部分不得留到下一次；三、冷却与恢复时长按登记值和当前急速折算，已经开始计时的冷却不因后续急速变化重算；四、全局冷却只阻止新的释放，不得改写任何技能自身的充能恢复进度；五、充能未满时按 recovery_ms 恢复一层，恢复恰好发生在到期时刻，早一毫秒都不行，单次恢复不得超过登记上限；六、use 被拒绝时不得扣层、不得推进全局冷却，denied 统计加一；七、暂停期间所有计时冻结，恢复时把每个到期时刻整体后移暂停时长；八、重复 pause 抛 CD_ALREADY_PAUSED，未暂停时 resume 抛 CD_NOT_PAUSED；九、时间不得倒退，处理时刻一旦推进就不允许回拨，now 小于上一次处理时刻抛 CD_STALE_TIME；十、重复登记抛 CD_DUPLICATE_SKILL，cooldown_ms 非正或 charges 小于一抛 CD_INVALID_ARGUMENT，未知技能抛 CD_UNKNOWN_SKILL；十一、快照保存每技能剩余冷却、充能数、恢复到期、急速、暂停时刻与统计，恢复后冷却不得缩短，结构非法抛 CD_SNAPSHOT_INVALID 且原状态不变。

硬约束：仅剩一层充能时并发 use 只能成功一次，其余必须拿到 CD_NO_CHARGES 或 CD_GLOBAL_COOLDOWN，充能数不得扣成负数；暂停跨越冷却结束点时，恢复后该技能应立即可用，不得补一段冷却；充能已满时恢复计时必须停止，不得把多余时间累积到下一次；stats 固定返回 uses、denied、recovered、pauses 四个整数，reset_stats 归零。

验收：python3 -m pytest -q tests 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；暂停跨结束点与并发使用自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 游戏开发
- type: Bugfix
- difficulty: 地狱
- module: cooldown_charge_tracker
- typeName: CooldownTracker
