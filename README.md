# batch48-py-game-a-12

## 题面

新赛季上线的观战与回放服务开始被带宽账单追着跑：每个连接每秒都在收整张场景的全量快照，明明只有血量和坐标在动，队友列表和背包字段也一起重发；高分段对局里本地玩家脚下的即时反馈反而排在远山上的树后面，等半秒才到；某一 tick 预算打满时，同一个实体的字段被切成两半发出去，客户端拼出一个不存在的状态；实体被删除后客户端还留着它的模型，直到另一条快照把它覆盖；断线重连后服务端把没被确认的旧基线当成已确认，脏位整体错位。范围是 gsb/batch48-py-game-a-12 的 snapshot_delta_encoder.py，类 SnapshotDeltaEncoder，负责每连接基线、脏字段掩码、优先级累加与每 tick 带宽切片。

公开接口：SnapshotDeltaEncoder(*, keyframe_interval=8, keyframe_age=32, field_cost=1, event_cost=1)；register_entity(entity_id, fields, *, local=False) -> dict；remove_entity(entity_id) -> dict；update_field(entity_id, name, value) -> dict；update_fields(entity_id, values) -> dict；set_local_player(entity_id, *, local=True) -> dict；set_distance(entity_id, distance) -> dict；encode(*, tick, budget) -> dict；ack(*, tick) -> dict；baseline_of(entity_id) -> dict | None；dirty_of(entity_id) -> list；entities() -> dict；stats() -> dict；reset_stats() -> None；snapshot() -> dict；restore(state) -> None。拒绝统一抛 DeltaError(code, detail)。

故障与边界：一、注册返回 entity 与 fields，重复注册抛 DELTA_DUPLICATE_ENTITY 且不改原值，未确认基线的实体所有字段都算脏，dirty_of 按键名升序返回列表；二、register_entity 追加一条 create 事件，remove_entity 追加一条 remove 事件，事件形如 type 与 entity 两字段且发出后清空待发队列，删除后基线为 None 且再更新抛 DELTA_UNKNOWN_ENTITY；三、update_field 的字段名必须已存在于该实体，否则抛 DELTA_UNKNOWN_ENTITY，update_fields 按键名排序后逐条应用；四、encode 固定返回 tick、keyframe、entities、events、used、deferred 六项，entities 每项含 entity 与 fields，used 为字段成本加事件成本；五、没有已确认基线、tick 能被 keyframe_interval 整除、或距上次确认已满 keyframe_age 个 tick 时走关键帧，关键帧发全部实体全部字段且不受预算限制；六、增量只发与已确认基线不同的字段，事件无条件先发并从预算里扣除，预算不足时整实体延后、字段不得拆到两个 tick，deferred 记延后实体数；七、优先级为本地玩家加 50、距离小于 100 加 30、小于 400 加 15、尚未确认基线加 20，同分按实体名升序；八、编码不清脏位，只有 ack 覆盖该字段后才清，因此未确认字段会在下一 tick 继续出现；九、ack 返回 tick 与 applied 实体列表，把该 tick 切片里的值写入已确认基线，从未编码过的 tick 抛 DELTA_UNKNOWN_TICK；十、entities 返回每个实体的 fields、local、distance 快照；十一、stats 固定返回 encoded、keyframes、fields、events、deferred、acks 六个整数，reset_stats 归零；十二、snapshot 深拷贝实体表、基线、脏位、待发事件与统计，顶层 version 当前为 1，restore 后基线脏位与编码结果一致，结构非法抛 DELTA_SNAPSHOT_INVALID 且不破坏原状态。

硬约束：register_entity、remove_entity、update_field、encode、ack 全程持锁，八路并发更新不同实体时脏位不得丢，字段计数必须等于实际发出的字段数；快照恢复后不得把未确认基线当成已确认。验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发与恢复场景自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 游戏开发
- type: Bugfix
- difficulty: 地狱
- module: snapshot_delta_encoder
- typeName: SnapshotDeltaEncoder
