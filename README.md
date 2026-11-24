# batch48-py-game-a-05

## 题面

规则集切换上线后，运营把新赛季配置激活，客服和运维同时炸了：已经开打的对局在加载下一张地图时换成了新规则，回放里一半回合按旧伤害算、一半按新伤害算；切换前已经进房但还没开的比赛，按房间创建时刻取了旧规则，开局后跟公告对不上；新规则集激活失败的赛季停在半套配置上，两张地图各用一套数值；回滚之后已钉扎旧规则的对局被迫换成更新的一版；还有回放服务遇到规则摘要不一致的对局，静默按当前版本重算，比分完全对不上。

范围是 gsb/batch48-py-game-a-05 的 season_ruleset_switcher.py，类 SeasonRulesetSwitcher，负责规则集登记、赛季激活、对局钉扎、回滚与摘要核对。

公开接口：SeasonRulesetSwitcher(*, max_rulesets=8, clock=None)；register_ruleset(name: str, *, season: str, payload) -> dict；ruleset_info(name: str, *, season: str, version: int | None = None) -> dict；activate(name: str, *, season: str) -> dict；active_for(season: str) -> dict；rollback(season: str) -> dict；create_room(room_id: str, *, season: str) -> dict；start_room(room_id: str) -> dict；pin_for_match(match_id: str, *, season: str) -> dict；resolve_for_match(match_id: str) -> dict；另有 match_state、finish_match、audit_mismatch、replay、stats、reset_stats、snapshot、restore，并提供 canonical_digest(payload)。拒绝统一抛 RulesetError(code, detail)。

故障与边界：一、规则集按赛季和名字唯一，同一份内容重复登记幂等，内容变化版本号加一，旧版本仍可按版本号取回；二、规则集总数受 max_rulesets 硬顶，超了报 RULESET_LIMIT，更新已有规则集不受限；三、同一赛季同一时刻只有一个活跃规则集，重复激活幂等且 generation 不变，换规则集 generation 加一；四、激活不存在的规则集必须整体失败，活跃规则集与历史都不动；五、create_room 只登记房间，start_room 才按开局时刻取活跃规则集并钉扎，房间重复创建报 RULESET_ROOM_EXISTS，重复开局报 RULESET_ROOM_ALREADY_STARTED，没有活跃规则集报 RULESET_NO_ACTIVE；六、钉扎后 digest 与版本号固定，resolve_for_match 的 payload 必须是深拷贝，对局结束后再改结果报 RULESET_MATCH_FINISHED，空结果报 RULESET_INVALID_RESULT；七、规则集内容摘要与对局记录不一致时要标记异常并拒绝重放，不能静默采用当前版本；八、回滚把活跃规则集退回上一版并让 generation 加一，没有历史报 RULESET_NOTHING_TO_ROLLBACK；九、切换瞬间已创建但未开局的房间要按开局时刻取规则集，不能按创建时刻；十、回滚后已钉扎旧规则集的对局继续用旧版，不得中途换规则；十一、换赛季时各赛季的活跃项与历史互不影响，取不到活跃项报 RULESET_NO_ACTIVE；十二、快照恢复后规则集版本、活跃项、历史、房间、对局与计数都要在，且不得与恢复前的对象共享结构。

硬约束：activate、rollback、pin_for_match、start_room 全程持锁，并发开局只能看到旧版或新版的完整规则，八路并发钉扎期间切换规则集后每个对局的 digest 与实际取回的 payload 必须一致；同一赛季的活跃规则集只能有一个，generation 单调递增；规则集版本只增不删，旧版本必须可按版本号还原；snapshot 必须深拷贝，restore 结构非法报 RULESET_SNAPSHOT_INVALID 且原状态不变。

验收：`python3 -m pytest -q tests` 的 15 条必须全过；不得修改测试、跳过或按固定输入写死；并发钉扎与快照恢复自行验证；只做本地实现，不涉及上游提交。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- name: batch48-py-game-a-05
- domain: 游戏、图形与多媒体
- sub: 游戏开发
- type: Bugfix
- difficulty: 困难
- module: season_ruleset_switcher
- typeName: SeasonRulesetSwitcher
