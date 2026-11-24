# batch48-py-game-a-19

## 题面

跨服场景切分上线后，玩家角色开始出现分身：一次跨服传送在两张图里同时看到同一个角色，旧图里那个不能攻击却一直站着；有的角色传过去后宠物丢了，旧服列表里还留着它，点开就报空引用；玩家在迁移途中断线重连，查询得到空结果，界面当成已删除，直接踢回登录服；迁移窗口内的移动指令全部丢失，落地后角色停在原地；回执超时的迁移本该退回源服，目标服却装进了迟到数据，客服只能手工删掉一个实例；迁移日志只在内存里，进程重启后窗口状态全丢。范围是 gsb/batch48-py-game-a-19 的 authority_handover_guard.py，类 AuthorityHandoverGuard，负责导出快照、导入校验、不可见窗口与输入序号转交。

公开接口：AuthorityHandoverGuard(*, receipt_timeout_ticks=3, max_buffered=64)；register_entity(entity_id, *, process, fields=None, children=()) -> dict；destroy_entity(entity_id) -> dict；begin_handover(entity_id, *, source, target, tick=0) -> dict；export_state(entity_id) -> dict；import_state(target, state) -> dict；deliver_receipt(ticket, receipt) -> dict；expire(tick) -> list；buffer_input(entity_id, seq, payload=None) -> dict；forward_inputs(ticket) -> dict；apply_input(process, entity_id, seq, payload=None) -> dict；query(entity_id) -> dict；invisible(entity_id) -> bool；authority(entity_id) -> str；handover(ticket) -> dict；stats() -> dict；reset_stats() -> None；snapshot() -> dict；restore(state) -> None。拒绝统一抛 HandoverError(code)。

要修的点：一、注册返回 entity、authority、children、state 与 fields，标识为空或子实体不存在抛 HANDOVER_INVALID_ARGUMENT，重复注册抛 HANDOVER_DUPLICATE；二、begin_handover 要求实体可见且 source 等于当前归属，归属不符抛 HANDOVER_WRONG_PROCESS，目标与源相同或为空抛 HANDOVER_INVALID_ARGUMENT，窗口已开再开抛 HANDOVER_INVALID_STATE，票据形如 ho-1 按开窗次数编号；三、开窗后实体在两端都不可见、归属仍在源服，deadline 等于开窗 tick 加 receipt_timeout_ticks，窗口状态为 migrating；四、export_state 只对进行中的窗口可用，返回 entity、ticket、authority、fields、children、tick 六项，fields 与 children 为深拷贝；五、import_state 先校验票据存在，目标不符返回 accepted 为假、reason 为 wrong_target；快照缺项、entity 或 ticket 与窗口不一致、tick 非法返回 reason 为 state_mismatch；快照引用的子实体在窗口内被销毁时导入必须整份失败回退，不得留下悬空引用；两种拒绝都计入 rejected，导入通过返回 accepted、ticket、entity、target 与形如 rc-ho-1 的 receipt；六、deliver_receipt 把归属切到目标并让实体在目标单方可见，返回 ticket、entity、outcome 为 moved、authority 与 invisible；未导入就交付回执抛 HANDOVER_INVALID_STATE，回执内容不符抛 HANDOVER_RECEIPT_INVALID；七、expire 只回退 tick 大于 deadline 且没有回执的窗口，回退后归属留在源服、实体恢复可见、窗口状态 rolled_back，返回被回退的票据列表，回退之后目标端迟到的导入数据必须丢弃，不能让实体在两处同时可见；八、迁移期间源侧按序号缓冲输入，重复序号只留一份，缓冲达到 max_buffered 抛 HANDOVER_INPUT_LIMIT；九、forward_inputs 只对已迁移完成的窗口可用，按序号升序返回 forwarded 与 count 并清空缓冲，重复调用返回空列表；十、apply_input 按序号去重应用，序号小于等于已应用值时 applied 为假、duplicate 为真，next_expected 为已应用最大值加一，进程不是当前归属抛 HANDOVER_WRONG_PROCESS，序号不是正整数抛 HANDOVER_INVALID_ARGUMENT；十一、query 对已销毁实体返回 state 为 absent、authority 为空，迁移窗口内的查询必须返回明确的迁移中状态，不能返回空当成已删除；十二、stats 固定返回 handovers、moved、rolled_back、rejected、inputs_forwarded、inputs_applied、inputs_duplicated 七个整数，reset_stats 归零但不清实体与窗口；snapshot 返回 version=1 的深拷贝，restore 后实体、窗口、票据编号、缓冲与统计一致，结构非法抛 HANDOVER_SNAPSHOT_INVALID 且不破坏当前状态。

硬约束：register、begin、import、deliver、expire、buffer、forward、apply 与快照读写全程持同一把锁，八路并发开窗只能有一个成功；快照跨进程恢复后不得与恢复前的对象共享可变结构；缓冲条数与迁移日志条数有上限，内存不随开窗次数无界增长。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发开窗、超时回退与重启恢复自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 游戏开发
- type: Bugfix
- module: authority_handover_guard
- typeName: AuthorityHandoverGuard
