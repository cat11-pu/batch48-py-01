# batch48-py-game-a-23

## 题面

团队副本的仇恨统计最近连续出现坦克被秒、治疗被小怪追着打的事故：治疗刷了十万血，仇恨表里却按十万整记，治疗量直接压过主坦；副坦按下嘲讽后，小怪依旧转身去打仇恨最高的输出；超出近战范围的目标仍被选中，小怪隔着半个场地追人；威胁值明明已经随时间衰减，表里的数字却只随事件次数变化；两个输出威胁完全相同时，每次选目标都在换人，录像无法复盘；已经阵亡的队员还挂在仇恨表里占位。范围是 gsb/batch48-py-game-a-23 下的 threat_table_selector.py，类 ThreatSelector，负责按敌对单位维护威胁表、处理嘲讽、按距离与时间衰减并给出当前目标。

公开接口：ThreatSelector(*, heal_factor: float = 0.5, melee_range: float = 5.0, swap_margin: float = 0.15, decay_per_second: float = 10.0)；add_mob(mob, *, position=(0.0, 0.0)) -> dict；add_source(mob, source, *, position=(0.0, 0.0)) -> dict；add_threat(mob, source, amount, kind, *, now=0.0) -> dict；select(mob, now) -> dict；taunt(mob, source, duration, *, now=0.0) -> dict；decay(mob, now) -> dict；kill(mob, source) -> dict；table(mob) -> list；threat(mob, source) -> float；snapshot() -> dict；restore(state) -> None；stats() -> dict；reset_stats() -> None。所有拒绝抛 ThreatError(code, detail)。

add_mob 返回 mob、position、sources，sources 为已登记来源数，add_source 返回 mob、source、position、alive，add_threat 返回 mob、source、amount、kind、threat、total，select 返回 mob、target、threat、reason，reason 取 taunt、highest、hysteresis、none，taunt 返回 mob、source、until，decay 返回 mob、decayed、now，kill 返回 mob、source、removed、sources；table 每项含 source、threat、distance、alive。

故障与边界：一、kind 只接受 damage 与 heal，heal 按 heal_factor 折算后入表，amount 非正抛 TH_INVALID_AMOUNT，非法 kind 抛 TH_INVALID_KIND；二、威胁按事件累加，初始威胁为零，table 按威胁降序并带距离与存活标记；三、选择目标只在存活、距离不超过 melee_range 且威胁大于零的来源里挑，全部不满足时 target 为 None、reason 为 none；四、嘲讽有效期内无视威胁强制选中嘲讽者，reason 为 taunt；五、嘲讽来源必须已登记在对应敌对单位下，否则抛 TH_UNKNOWN_SOURCE，duration 非正抛 TH_INVALID_DURATION；六、decay 按经过秒数乘 decay_per_second 扣减，同一时刻重复调用不得二次扣减，威胁最低为零，时间倒退抛 TH_STALE_TIME；七、当前目标仍在范围内且存活时，挑战者威胁必须超过当前目标威胁乘一加 swap_margin 才换目标，否则 reason 为 hysteresis，换目标时 reason 为 highest，当前目标走出近战范围则立即改选范围内最高威胁；八、kill 后来源从表中移除并不再参与选择，重复 kill 幂等且 removed 为 False；九、敌对单位或来源未登记分别抛 TH_UNKNOWN_MOB、TH_UNKNOWN_SOURCE；十、快照保存威胁值、来源坐标与存活标记、嘲讽到期时刻、当前目标与统计，恢复后同一时刻选择结果逐字段一致，结构非法抛 TH_SNAPSHOT_INVALID 且原状态不变。

硬约束：add_threat 与 select 交错执行时，被选中的目标必须确实存在于该敌对单位表中且处于存活状态，不能返回半个更新；嘲讽到期瞬间要按当时的威胁表重新选择，而不是回退到嘲讽前的目标；威胁值相同的多个来源必须用稳定可复现的次序决胜，不能依赖字典插入顺序；stats 固定返回 threats_added、heals、selects、taunts、decays 五个整数，reset_stats 归零。

验收：python3 -m pytest -q tests 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；嘲讽到期与并发选择自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 游戏开发
- type: Bugfix
- difficulty: 地狱
- module: threat_table_selector
- typeName: ThreatSelector
