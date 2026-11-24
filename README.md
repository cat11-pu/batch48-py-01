# GSB 任务说明

## 题面

排位赛的暂停功能这周被大量投诉：一支队伍连续点了三次暂停，第三次没有报错却把整局卡死在暂停里；两支队伍几乎同时提交暂停申请，裁判把后到的申请判成“已经在暂停中”，之后谁也恢复不了；暂停倒计时结束后画面仍然冻结，只能等运维手动踢人；有队伍趁暂停期间提交换人，恢复后名单直接多出一个人；还有对局恢复后，上一份提案没投完的票被算进了下一次申请，新提案一提交就通过。客服统计时还发现，每队每局的暂停配额在服务重启后回到满值，单次暂停时长也没有硬性上限。

范围是 gsb/batch48-py-game-a-09 的 match_pause_referee.py，类 PauseReferee，负责队伍名单、暂停申请与投票、暂停状态机、配额与超时恢复、换人请求排队和快照恢复。

公开接口：PauseReferee(*, quorum=2, quota_per_match=2, max_duration_ms=60000, max_players=8, clock=None)；register_team(team, players) -> dict；roster(team) -> list；quota_of(team) -> int；request_pause(team, *, player, duration=None, now=None) -> dict；vote_pause(proposal_id, *, player, now=None) -> dict；resume(proposal_id, *, player=None, now=None) -> dict；force_resume(*, reason="", now=None) -> dict；request_substitution(team, *, out_player, in_player, now=None) -> dict；state(*, now=None) -> dict；proposal_of(team) -> dict | None；stats() -> dict；reset_stats() -> None；snapshot() -> dict；restore(state) -> None。拒绝统一抛 PauseError(code, detail)。

故障与边界：一、队伍名单必须非空且不重复，超过 max_players 拒绝，同名队伍名单不同按冲突拒绝，名单一致返回 duplicate；二、申请暂停必须是本队名单内玩家，暂停中再申请直接拒绝，配额为零拒绝；三、同一队伍在提案待投票期间重复申请必须返回同一个 proposal_id 和 pending_duplicate，不得新建提案；四、单次时长超过 max_duration_ms 拒绝，非正整数拒绝，不传时按硬顶计；五、提案发起人算第一票，达到 quorum 才进入暂停，进入暂停时扣减该队配额并记录结束时刻；六、非本队玩家投票拒绝，重复投票返回 duplicate_vote 且票数不变，已关闭提案再投票按关闭拒绝，已生效提案再投票返回 already_active；七、暂停期间不允许任何队伍发起新申请，暂停生效前的并发投票要合并到同一提案，不能产生两个暂停状态；八、恢复只能在暂停中且只对当前生效提案有效，恢复后提案关闭、配额不再退还；九、超时由裁判在读取状态时自动恢复并留痕，自动恢复同样消耗配额，恢复前一毫秒仍应保持暂停；十、每队每局暂停次数与单次时长都有硬顶；十一、暂停期间发生的换人请求要排队到恢复后处理，不能直接修改参战名单，运行中换人立即生效，目标玩家已在名单内按冲突拒绝；十二、强制恢复后原提案剩余票数必须清零，不能带到下一次申请；snapshot 深拷贝名单、提案、配额与排队请求，restore 后暂停截止时刻与配额一致，结构非法拒绝且不破坏原状态。

硬约束：申请、投票、恢复与换人全程持锁，多线程同时申请同一队伍的暂停只能产生一个待投票提案；快照跨进程恢复后继续恢复/暂停不丢状态；队伍人数、每队配额、单次时长与待处理换人数量都有上限，不允许无限增长。

验收：`python3 -m pytest -q tests` 的 15 条测试全部通过；不得修改测试、跳过或按固定输入写死；并发与重启场景自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 游戏开发
- type: Bugfix
- difficulty: 地狱
