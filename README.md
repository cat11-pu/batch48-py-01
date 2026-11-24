# batch48-py-game-b-06

## 题面

实例化渲染在最近两个版本里频繁掉帧，负责合并绘制的同学定位到槽位与批次台账已经不可信：新刷出来的实例有时套用上一个实例的旧属性，删掉对象后槽位空着不复用，同一批材质被拆成十几段提交；美术在同一个场景里反复增删特效实例后，单帧上传区间碎成几十段，绘制调用数也跟着上涨，抓帧时还会看到本该早已释放的槽位仍在提交旧数据；更糟的是一次整理之后，按编号查实例拿到的是隔壁对象的数据。范围是 gsb/batch48-py-game-b-06 下的 instance_batch_guard.py，类 InstanceBatchGuard，负责实例槽位分配、属性脏区间合并、绘制批次切分、碎片整理和快照恢复。

公开接口：`InstanceBatchGuard(*, max_slots: int = 256, max_batches: int = 64, merge_gap: int = 0)`；`create(instance_id, *, mesh, material, attrs=None) -> dict`；`update(instance_id, attrs) -> int`；`delete(instance_id) -> int`；`slot_of(instance_id) -> int`；`instance_at(slot) -> str | None`；`dirty_ranges() -> list[tuple[int, int]]`；`commit() -> list[tuple[int, int]]`；`batches() -> list[dict]`；`fragmentation() -> float`；`compact() -> dict`；`snapshot() -> dict`；`restore(state) -> None`；`stats() -> dict`；`reset_stats() -> None`。冲突一律抛 InstanceBatchError(code, detail)。

故障与边界：一、create 取当前最小空闲槽位，槽位号从 0 开始，返回字典只含 instance_id 与 slot 两个键；instance_id 重复抛 DUPLICATE_INSTANCE 且不计入 created；attrs 不是字典、mesh 或 material 为空串抛 ATTR_INVALID。二、槽位用尽抛 SLOT_LIMIT，不落任何状态，不留半条实例记录。三、delete 释放槽位并返回槽号，之后 create 必须优先复用编号最小的空闲槽；对已删除或不存在的标识再查抛 UNKNOWN_INSTANCE。四、create 与 update 都把所在槽标记为脏，update 返回槽号且不改动批次归属；dirty_ranges 返回按槽位升序、左闭右开、相邻脏槽合并后的区间，空槽间隔不超过 merge_gap 时也要并成一段。五、commit 返回本次合并结果并清空脏标记，重复调用返回空列表，committed_ranges 按返回的段数累加。六、batches 按槽位升序切分，mesh 或 material 变化、槽位不连续都断开，每项含 mesh、material、start、end；插入新实例后段数超过 max_batches 抛 BATCH_LIMIT，且实例不创建、槽位不占用。七、fragmentation 返回已用跨度（最大已用槽号加一）内的空闲槽占比，没有实例时为 0.0。八、compact 按原槽号升序把实例重排到 0..n-1，返回 moved 与 slots，并把全部在用槽标脏；整理期间读方按 instance_id 查槽必须拿到一致结果，不能读到半完成的映射。九、同一帧内对同一实例多次 update，只按最后一次的属性值合并成一次上传。十、槽位被复用前必须清掉残留脏标记，新实例不得继承旧脏区间。十一、snapshot 返回 version、slots（槽号为键，每项含 instance_id、mesh、material、attrs、dirty）与 stats 的深拷贝，改动返回值不得影响实例；restore 接受非法结构时抛 SNAPSHOT_INVALID 且原状态不变，合法时整体替换并保留待上传脏区间。十二、stats 固定六个整数计数：created、deleted、committed_ranges、commits、compactions、moved_slots，reset_stats 只清计数，不清实例与待上传区间。

硬约束：create、update、delete、commit、compact 全程持锁，十路并发各自创建同一网格的实例时槽位不得重复、created 必须等于成功次数；快照跨进程恢复后不得与恢复前的结构共享可变对象；槽位数与批次数都有上限，禁止无界增长。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发与恢复场景自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 图形渲染
- type: Bugfix
- module: instance_batch_guard
- typeName: InstanceBatchGuard
