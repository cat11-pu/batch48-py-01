# batch48-py-game-a-21

## 题面

线上回合制战斗服的伤害结算从上周起被玩家集中投诉：同一段战斗录像重放两次，伤害数字对不上；技能明明应该先出手，结算却按动作名排序；已经倒下的单位还能在本回合再砍一刀；闪避提示已经弹出，血条照样往下掉；格挡动作做完，暴击伤害反而比不格挡时更高；一个群体技能打在同属性小怪身上，整组小怪要么一起暴击要么一起被闪避。战斗服重启后再回放，从第三回合起所有随机结果整体偏移。范围是 gsb/batch48-py-game-a-21 下的 combat_round_resolver.py，类 CombatRoundResolver，负责登记战斗单位、按优先级结算一回合、产出可重放的战斗日志，并处理并发提交。

公开接口：CombatRoundResolver(*, seed: int = 0, max_rounds: int = 999)；define_unit(unit_id, *, hp, attack, defense, dodge=0.0, block=0.0, crit=0.0, priority=0, targets=1) -> dict；resolve_round(actions, *, seed=None, round_no=None) -> dict；roll_check(attacker, defender, kind) -> dict；apply_order() -> list；replay(log) -> dict；unit_state(unit_id) -> dict；remove_unit(unit_id) -> dict；snapshot() -> dict；restore(state) -> None；stats() -> dict；reset_stats() -> None。所有拒绝抛 CombatError(code, detail)，code 取下面列出的字符串。

unit_state 返回 unit、hp、max_hp、attack、defense、dodge、block、crit、priority、targets、alive 十一个字段；日志每条记录含 actor、kind、target、priority、hit、dodged、crit、blocked、damage、hp_before、hp_after，totals 固定含 damage、hits、dodges、crits、blocks 五个整数。

故障与边界：一、动作按 priority 降序结算，priority 相同保持提交次序，缺省 priority 由 kind 决定，skill 高于 attack、attack 高于 item、item 高于 move，apply_order 返回该缺省次序；二、闪避、格挡、暴击概率取值零到一，hp 必须为正，单位标识为空同样非法，重复登记抛 CR_DUPLICATE_UNIT，非法数值抛 CR_INVALID_UNIT；三、每次攻击对每个目标分别判定闪避、暴击、格挡三次掷值，闪避成立时伤害为零且不扣血；四、基础伤害为攻击力加动作威力减防御且最低一，暴击乘二，格挡在暴击之后按一半向下取整且最低一；五、已死亡单位提交的动作跳过并在日志留 status 为 dead_actor 的记录，未知单位抛 CR_UNKNOWN_UNIT，空动作列表抛 CR_EMPTY_ACTIONS，非法动作类别抛 CR_INVALID_ACTION；六、同一 seed 与同一动作序列必须产出逐字段相同的日志，换一台机器、换一个进程同样成立；七、日志必须保存每个目标的掷值与结算前后血量，replay 依据日志重算并校验，篡改伤害抛 CR_LOG_TAMPERED；八、roll_check 的 kind 只接受 hit、dodge、block、crit，返回 kind、chance、roll、success 四个字段，同一实例连续调用的号码序列可复现；九、结算序号小于等于已结算回合抛 CR_ROUND_COMMITTED，跳号抛 CR_ROUND_GAP；十、快照保存随机源状态、单位状态与已结算回合号，恢复后继续结算与直接结算逐字段一致，结构非法抛 CR_SNAPSHOT_INVALID 且原状态不变。

硬约束：同一场战斗并发提交同一回合只能有一个成功，其余必须拿到 CR_ROUND_COMMITTED，不能两次写入同一回合号；状态变更全程在实例锁内完成；stats 固定返回 rounds、actions、hits、dodges、crits、blocks、damage 七个整数，reset_stats 归零。

验收：python3 -m pytest -q tests 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发提交与重启恢复场景自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 游戏开发
- type: Bugfix
- difficulty: 地狱
- module: combat_round_resolver
- typeName: CombatRoundResolver
