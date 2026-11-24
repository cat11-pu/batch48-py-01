# batch48-py-game-a-06

## 题面

赛季收口这周工单堆在一起：客户端重连后把同一场对局又上报一次，玩家评分被加了第二遍，赛季场次也多记一场；同一场对局两次上报的分数不一样时，账本按后到的版本覆盖，客服翻不出第一次结算的凭证；补传的旧对局按入库时间归进新赛季，老赛季榜单迟迟收不了口；运营更正一场漏算的对局后，历史里的原分录被改掉，审计对不上；作废一场已结算的对局，分数没回到结算前；服务重启后部分玩家的累计分与历史分录合计对不上，个别赛季场次归零；扣到负分的玩家余额显示为零，历史里看不出扣了多少；单场人数多的对局写到一半报错，剩下的玩家既没入账也没有提示。

范围是 gsb/batch48-py-game-a-06 的 match_result_ledger.py，类 MatchResultLedger，负责对局结算、赛季归属、冲正更正、作废与快照恢复。

公开接口：MatchResultLedger(*, season_starts=None, base_rating=1000, max_entries=5000, clock=None)；resolve_season(ended_at) -> str；submit_result(match_id, *, ended_at, results) -> dict；amend_result(match_id, *, results, reason="") -> dict；void_match(match_id, *, reason="") -> dict；rating_of(player, *, season) -> int；history_of(player, *, season=None) -> list；settlement_of(match_id) -> dict；season_matches(season) -> int；match_state(match_id) -> str；stats() -> dict；reset_stats() -> None；snapshot() -> dict；restore(state) -> None。拒绝统一抛 MatchLedgerError(code, detail)，错误码包括 LEDGER_INVALID_RESULT、LEDGER_UNKNOWN_SEASON、LEDGER_UNKNOWN_MATCH、LEDGER_ENTRY_LIMIT、LEDGER_SNAPSHOT_INVALID。

故障与边界：一、赛季按 season_starts 的起始时刻划分，边界时刻归后一赛季；ended_at 为负报 LEDGER_INVALID_RESULT，早于首个赛季起点报 LEDGER_UNKNOWN_SEASON；二、results 以玩家名为键，每项含 delta 与 win，match_id、玩家名不得为空，ended_at 非负，delta 为整数且不能是布尔，win 必须显式给出，否则整单按 LEDGER_INVALID_RESULT 拒绝，不落分录只计 rejected；三、同一 match_id 内容完全重复返回 duplicate，带回首次的 ratings 与 entry_count，不新增分录，也不重复累计赛季场次；四、同一 match_id 内容不同按冲突拒绝并保留首次结果，冲突计入 conflicts，原分录与余额不动；五、迟到结果按 ended_at 归属赛季，不按到达顺序，先到的新赛季结果不被后到的旧赛季结果改写，各赛季余额分开累计；六、首次结算返回 match_id、season、status、players、ratings、entry_count、revision，match_state 至少区分 unknown、settled 与 amended，分录只追加，每条带自增 seq、match_id、kind、delta、balance_after；七、rating_of 对未知玩家或未知赛季返回 base_rating，history_of 默认返回该玩家全部赛季分录，结果必须是深拷贝，调用方改动不影响账本；八、更正返回 status 与 amendment 序号，revision 加一，先追加与原文等额反向的 amend_reversal，再追加新的 amend 分录，settlement_of 的 deltas 反映净变化，更正玩家集合必须与首次结算一致，只改部分玩家报 LEDGER_INVALID_RESULT；九、作废已结算对局追加反向分录使余额回到结算前，原分录不删，voided 加一，重复作废或未知对局报 LEDGER_UNKNOWN_MATCH；十、评分下限为零，扣穿时余额停在零但分录保留原始 delta，后续对局从零继续累计；十一、分录总数受 max_entries 硬顶，一次结算新增后超限整单拒绝并报 LEDGER_ENTRY_LIMIT，该对局保持未知，余额与 entries 不变，只计 rejected；十二、stats 固定给出 accepted、duplicates、conflicts、amended、voided、entries、rejected，其中 entries 含冲正与更正，reset_stats 只清计数，snapshot 带 version=1，深拷贝按赛季分组的余额、对局、分录、赛季场次与统计，restore 后余额、历史与计数一致，结构非法报 LEDGER_SNAPSHOT_INVALID 且原状态不变。

硬约束：结算、更正、作废与查询全程持锁，多线程同时提交同一 match_id 只能入账一次，其余返回 duplicate，八路并发下评分不得重复累计；snapshot 跨进程恢复后继续结算不重复也不丢分录，账本分录总数受 max_entries 硬顶，不允许无限增长。

验收：`python3 -m pytest -q tests` 的 15 条测试全部通过；不得修改测试、跳过或按固定输入写死；并发与重启场景自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- name: batch48-py-game-a-06
- domain: 游戏、图形与多媒体
- sub: 游戏开发
- type: Bugfix
- difficulty: 困难
- module: match_result_ledger
- typeName: MatchResultLedger
