# batch48-py-game-a-29

## 题面

上线第一个 CD 之后，任务面板的工单堆成山：一队五个人打同一只精英，每个人的击杀数都跳了五下，队伍共享目标瞬间溢出；目标早就超过需求，面板上的数字还在涨；交任务时背包里少一件材料，材料被扣走、任务却仍显示未完成；有人放弃任务后重新接取，旧进度又冒出来；同一份奖励在邮件里发了两遍；共享任务的进度只有队长能看到，队员面板一直停在零；客服按来源核对，发现击杀流水没有事件编号，谁打的、算没算过都查不到。范围是 gsb/batch48-py-game-a-29 下的 quest_objective_ledger.py，类 ObjectiveLedger，负责目标定义、接取、多来源进度合并、计数封顶、交付、放弃与快照恢复。

公开接口：`ObjectiveLedger()`；`DefineQuest(quest, objectives, *, turn_in=None, reward=None) -> dict`；`Accept(player, quest) -> dict`；`Record(player, quest, objective, delta, source) -> dict`；`Progress(player, quest) -> dict`；`TurnIn(player, quest, items) -> dict`；`Abandon(player, quest) -> dict`；`Sources(player, quest, objective) -> list`；`FormParty(party, players) -> dict`；`Snapshot() -> dict`；`Restore(state) -> None`；`Stats() -> dict`。拒绝统一抛 QuestLedgerError(code, detail)。

故障与边界：一、任务定义含 objectives 与可选 turn_in、reward，目标需求必须为正整数，空定义、非正整数或重复定义分别抛 QUEST_DEFINITION_INVALID、QUEST_ALREADY_DEFINED，定义返回的 objectives 归一成名称到需求数的映射；二、未定义任务一律抛 QUEST_UNKNOWN；三、进行中与已交付的任务再次接取抛 QUEST_ALREADY_ACCEPTED，进度保持不变，放弃后允许重新接取，计数从零开始；四、Record 的 delta 必须为正整数，否则抛 QUEST_DELTA_INVALID；source 必须是含非空 kind 与 event_id 的字典，否则抛 QUEST_SOURCE_INVALID；五、同一目标的同一 event_id 只计一次，重复记录返回 counted 为 False 且不增加计数、不写来源；六、目标计数达到 required 后不再增长，返回 capped 为 True，不能溢出；七、未接取或非进行中的任务记录进度抛 QUEST_NOT_ACCEPTED 或 QUEST_NOT_ACTIVE；八、队伍共享目标要按击杀事件标识去重，同一事件对同一成员只计一次，FormParty 不允许同一玩家同时属于两个队伍，冲突抛 QUEST_PARTY_CONFLICT；九、交付先校验目标是否全部完成、提交物品与 turn_in 是否完全一致，任何一项不满足都整体失败，物品不足抛 QUEST_ITEMS_MISMATCH 且进度与状态不变，不允许先扣物品再失败；十、交付成功只发一次奖励，重复交付抛 QUEST_ALREADY_DELIVERED，rewards 不增加；十一、放弃会清空计数与来源明细，重复放弃返回同一结果且 abandoned 只计一次；十二、Progress 返回 player、quest、state 与 objectives，每个目标含 count、required；Sources 按记录顺序返回来源副本；快照深拷贝并保存计数、来源明细与交付状态，恢复结构非法抛 QUEST_SNAPSHOT_INVALID 且原状态不变。DefineQuest 返回 quest、objectives、turn_in、reward；Accept 与 Progress 返回 player、quest、state、objectives；Record 返回 player、quest、objective、count、required、capped、counted；TurnIn 返回 player、quest、state、reward；Abandon 返回 player、quest、state。Stats 返回 accepted、recorded、delivered、abandoned、rewards 五个整数。

硬约束：同一玩家同一任务并发 Record 不得超计，全部接口共用一把可重入锁；交付的校验与状态切换必须在同一临界区内完成，不能出现扣了物品却没交付的中间态；快照恢复后不得重复发奖，已交付状态与来源明细必须一并恢复。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；队伍去重与并发交付自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 游戏开发
- type: Bugfix
- module: quest_objective_ledger
- typeName: ObjectiveLedger
