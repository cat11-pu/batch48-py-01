# batch48-py-game-a-11

## 题面

上个赛季更新后远端角色的移动开始发抖：观战里同一个敌人偶尔往回抽一帧再弹回来，网络越差越明显；有玩家反馈角色被击杀复活的瞬间会从尸体位置一路滑到出生点，中途穿过整张地图；掉线重连后画面直接定格，等服务器补包才恢复，期间队友位置也不再变化；传送类技能落地后镜头会被拉回释放前的位置再甩出去。抓包看，客户端把收到的快照按到达顺序堆在队列里，渲染时只用最新一条，既没有按服务端时间轴对齐，也没有处理实体生灭和传送这类跳变。范围是 gsb/batch48-py-game-a-11 的 snapshot_interp_guard.py，类 SnapshotInterpGuard，负责快照入库、时间轴采样、实体生灭边界、传送吸附以及缓冲与实体状态的存档恢复。

公开接口：SnapshotInterpGuard(*, buffer_size=32, max_extrapolation=250)；ingest(snapshot) -> dict；sample(render_time) -> dict；teleport(entity_id, x, y, at_time) -> dict；despawn(entity_id, at_time) -> dict；entity_state(entity_id) -> dict | None；bounds() -> tuple；stats() -> dict；reset_stats() -> None；snapshot() -> dict；restore(state) -> None。拒绝统一抛 InterpError(code, detail)。

故障与边界：一、快照含 server_time、seq、entities 三段，实体至少给 x、y，vx、vy 缺省为 0.0，缺段或 entities 非字典抛 INTERP_BAD_SNAPSHOT 并计入 rejected；二、入库先按 seq 去重，同一 seq 只保留首次内容，重复返回 accepted 为 False、reason 为 duplicate，不增加 ingested；三、server_time 早于最近入库快照抛 INTERP_TIME_REGRESSION，缓冲不变，同一时间戳不按回退处理；四、入库成功返回 accepted 为 True 与当前条数 buffered，缓冲满后淘汰最旧快照，bounds 返回最旧时间、最新时间与条数；五、缓冲为空时采样抛 INTERP_EMPTY；六、render_time 落在两条快照之间时按时间间隔线性插值，mode 为 interp，早于最旧快照输出最旧坐标、mode 为 clamp；七、晚于最新快照按该快照速度外推，差值不超过 max_extrapolation 时 mode 为 extrap，超过后冻结在外推上限对应坐标、mode 为 frozen；八、实体只在区间右端出现时直接取右端坐标，不从空值插值；九、despawn 返回 entity 与 at，render_time 大于等于该时刻的采样不再输出该实体；十、teleport 返回 entity、at、x、y，直到出现 server_time 严格大于传送时刻的快照前，该实体固定输出传送坐标且 snapped 为 True；十一、sample 返回 mode 与 entities，每个实体含 x、y、snapped，正常插值 snapped 为 False，并计入 samples，entity_state 返回最新快照里该实体的字段拷贝，未知实体返回 None；十二、snapshot 深拷贝缓冲、实体表、传送与销毁记录及统计，顶层 version 当前为 1，restore 后 bounds 与采样一致，结构非法抛 INTERP_SNAPSHOT_INVALID 且不破坏原状态。

硬约束：ingest、sample、teleport、despawn 全程持锁，八路并发采样时不得抛异常且 samples 与调用次数完全相等；快照恢复后时间轴连续，实体不得停留在过期坐标。stats 固定返回 ingested、duplicates、rejected、samples、teleports、despawns 六个整数，reset_stats 归零。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发与恢复场景自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 游戏开发
- type: Bugfix
- difficulty: 地狱
- module: snapshot_interp_guard
- typeName: SnapshotInterpGuard
