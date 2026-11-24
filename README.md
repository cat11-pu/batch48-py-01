# batch48-py-game-a-13

## 题面

格斗服切到新的联机方案后客服收到一串回放投诉：本地玩家按下的招式当场放出来，半秒后角色又被拉回原位重新放一次，动作音效连着响两遍；一次严重的瞬移之后血量突然跳回几帧前的数值，之后整局都对不上；某些对局里客户端和服务器逐帧摘要早就不一致，直到结算才发现；还有玩家吐槽按键偶尔没反应，实际是服务器确认的输入被本地预测覆盖掉了；回滚窗口设得过大时，一次迟到的确认输入会把整局重新算一遍，观战端直接卡成幻灯片，重连后摘要校验形同虚设，两边越走越远。范围是 gsb/batch48-py-game-a-13 的 rollback_netcode_guard.py，类 RollbackNetcode，负责预测帧推进、远端输入回滚重模拟、回滚窗口上限、摘要校验与状态存档。

公开接口：RollbackNetcode(*, window=8)；predict(frame, input) -> dict；on_remote_input(frame, input) -> dict；state() -> dict；current_frame() -> int；confirmed_frame() -> int；digest(frame) -> int；on_server_digest(frame, digest) -> dict；window_size() -> int；reset() -> None；stats() -> dict；reset_stats() -> None；snapshot() -> dict；restore(state) -> None。拒绝统一抛 RollbackError(code, detail)。

故障与边界：一、帧号从 1 开始，predict 只能推进当前帧的下一帧，否则抛 ROLLBACK_FRAME_GAP 且不改变状态，成功返回 frame、pos、confirmed；二、每帧有效输入优先取已确认远端值，没有才用本地预测，pos 为逐帧累加；三、晚于当前帧的远端输入先暂存并返回 status 为 buffered 与 frame，暂存不改变当前帧与 pos，之后 predict 到该帧直接采用已确认值；四、与当前有效输入相同的远端输入返回 status 为 none 与 frame，不计回滚；五、不同则从该帧回滚并重模拟到当前帧，返回 rolled_back、from、depth、pos，depth 为当前帧减回滚帧；六、回滚深度等于窗口上限时仍然允许，超过上限则放弃回滚、返回 resync 并整体采信服务器状态；七、重模拟时已确认输入逐帧优先，不得重写更早的已确认区间；八、一次性事件按帧号登记，同一帧重模拟不得再次触发；九、digest 返回该帧摘要整数，on_server_digest 返回 status、frame、local、remote，不一致时 status 为 divergent 并计入 divergences，校验本身不得改变时间轴；十、confirmed_frame 返回从 1 起连续已确认的最高帧，没有则为 0，state 返回 frame 与 pos；十一、stats 固定返回 predicts、rollbacks、rolled_frames、resyncs、divergences、buffered，rolled_frames 累计重模拟帧数且单次为 depth 加 1，reset 清空时间轴回到帧 0 与 pos 0 但保留统计；十二、snapshot 深拷贝帧缓冲、已确认输入、摘要与统计，顶层 version 当前为 1，restore 后 state、confirmed_frame 与 digest 一致，结构非法抛 ROLLBACK_SNAPSHOT_INVALID 且不破坏原状态。

硬约束：predict、on_remote_input、on_server_digest 全程持锁，八路并发提交同一帧同一远端输入只能回滚一次，rolled_frames 不得重复累加；快照恢复后时间轴连续，已确认游标不得回退。验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发与恢复场景自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 游戏开发
- type: Bugfix
- difficulty: 地狱
- module: rollback_netcode_guard
- typeName: RollbackNetcode
