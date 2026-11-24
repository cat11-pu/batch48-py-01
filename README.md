# batch48-py-game-a-14

## 题面

大区合并后重连相关投诉集中爆发：玩家在地铁里断网十几秒，回来后角色先按旧位置走了几步再被拽回出生点；断线前发出的那几帧输入在重连后被当成新输入重放，同一个技能放了两遍；有人反复点重连，服务器每次都给一个新会话，旧连接的迟到包还在往新会话里灌数据；基线包和输入批同时到达时，客户端先把输入按旧基线应用，收到基线再回填，结果位置整体错一帧；还有一批异常客户端把几十秒的输入一次性塞过来，直接把重放缓冲打爆。范围是 gsb/batch48-py-game-a-14 的 resume_alignment_guard.py，类 ResumeAlignmentGuard，负责会话纪元、基线安装、重连输入重放去重、越窗拒绝与重连限流。

公开接口：ResumeAlignmentGuard(*, replay_window=32, max_reconnects=3, reconnect_window=10, clock=None)；begin_session(account) -> dict；close_session(account) -> dict；install_baseline(account, *, epoch, frame, state) -> dict；accept_batch(account, batch) -> dict；applied_inputs(account) -> list；applied_frame(account) -> int；baseline_of(account) -> dict | None；session_epoch(account) -> int；fired_events(account) -> list；stats() -> dict；reset_stats() -> None；snapshot() -> dict；restore(state) -> None。拒绝统一抛 ResumeError(code, detail)。

故障与边界：一、begin_session 返回 account 与 epoch，纪元从 1 单调递增，新会话清空该账号的基线与已应用输入，session_epoch 未开过会话返回 0；二、同一账号在 reconnect_window 秒内最多发起 max_reconnects 次重连，clock 用于取当前秒数，超出抛 RESUME_RATE_LIMITED 且不改变当前纪元；三、install_baseline 要求 epoch 等于当前未关闭的会话，否则抛 RESUME_STALE_EPOCH，成功返回 account、frame、epoch 并设定权威帧；四、batch 含 epoch 与 frames，每帧含 frame、input、events，按帧号升序应用，返回 status 与 accepted，至少应用一帧时 status 为 applied，全部未应用时为 stale；五、帧号不大于权威帧且已应用过的记为 duplicates，不大于权威帧但从未应用的记为 stale，两者都不改变权威帧，权威帧只由成功应用的帧推进；六、任一帧号大于权威帧加 replay_window 时整批拒绝、抛 RESUME_WINDOW_EXCEEDED 且 detail 里 require_baseline 为 True，已应用集合不变；七、帧号恰好等于权威帧加 replay_window 仍接受；八、一次性事件按帧登记，重复重放同一帧不得再次触发，fired_events 按应用顺序返回 frame 与 event；九、迟到包按会话纪元丢弃，epoch 不是当前会话或会话已关闭抛 RESUME_STALE_EPOCH 并计入 discarded；十、未安装基线时输入批返回 held 并暂存，基线装好后按序应用，不得先按旧基线应用再回填；十一、stats 固定返回 sessions、baselines、batches、accepted、duplicates、stale、rejected、events、discarded 九个整数，reset_stats 归零；十二、snapshot 深拷贝会话纪元、基线、已应用输入、事件、限流记录与统计，顶层 version 当前为 1，restore 后可继续接收同纪元输入，结构非法抛 RESUME_SNAPSHOT_INVALID 且不破坏原状态。

硬约束：begin_session、install_baseline、accept_batch 全程持锁，八路并发提交同一批输入只能应用一次，accepted 与 duplicates 之和必须等于帧数；快照恢复后纪元、基线与限流计数不得回退。验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发与恢复场景自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 游戏开发
- type: Bugfix
- difficulty: 地狱
- module: resume_alignment_guard
- typeName: ResumeAlignmentGuard
