# batch48-py-game-b-09

## 题面

雨后水面的倒影和玻璃幕墙在部分机型上出现前后层错乱，同一帧里半透明物体的叠放顺序在 PC 与手机上不一致；美术删掉一个特效再重新生成同 id 的实例后，画面在两帧之间闪烁；镜头拉开时整片植被的绘制顺序每帧都在抖，排序耗时冲进了主线程预算。范围是 gsb/batch48-py-game-b-09 下的 transparent_sort_guard.py，类 TransparentSortGuard，负责 64 位排序键、深度桶量化、增量重排与预算降级。

公开接口：`TransparentSortGuard(*, depth_bits=24, depth_min=0.0, depth_max=1000.0, max_objects=1024, sort_budget=64)`；`add(object_id, *, layer=0, material="default", double_sided=False, depth=0.0) -> dict`；`remove(object_id) -> None`；`update_depth(object_id, depth) -> int`；`bucket_of(object_id) -> int`；`key_for(object_id, *, side=0) -> int`；`keys(object_id) -> list[int]`；`object_count() -> int`；`sort() -> list[dict]`；`snapshot() -> dict`；`restore(state) -> None`；`stats() -> dict`；`reset_stats() -> None`。冲突一律抛 TransparentSortError(code, detail)。

故障与边界：一、排序键为 64 位无符号整数：最高 8 位放 layer，次 12 位放 material 批次（按首次出现顺序从 0 分配），再往下 depth_bits 位放深度桶反码，即最大桶号减当前桶号，保证远物键小，低 44 减 depth_bits 位放稳定序号；稳定序号按 2 递增，双面材质 side 为 1 的键等于 side 为 0 的键加一。二、深度桶按 depth 减 depth_min 乘最大桶号除深度区间后向下取整，depth 超出 depth_min 到 depth_max 抛 SORT_DEPTH_OUT_OF_RANGE；depth_bits、区间、对象上限与排序预算非法抛 SORT_CONFIG_INVALID。三、sort 按键升序返回，先 layer 后 material 批次再深度桶最后稳定序号，键小的先画；同桶对象保持插入顺序。四、双面材质拆成两条绘制，两条键必须相邻且 side 为 0 在前，单面材质请求 side 为 1 抛 SORT_SIDE_INVALID。五、任何两个绘制条目都不允许出现完全相同的排序键，否则不同平台会给出不同顺序。六、没有变化的帧直接复用上一帧顺序，reused_order 加一；有深度桶变化时做增量重排，脏桶要移到全局正确位置并修正相邻桶，不能只在桶内排序。七、需要重排的变动对象数超过 sort_budget 时降级为按 layer、material 批次、深度桶、object_id 的粗排，degraded 加一。八、对象删除后稳定序号不得立即复用，同帧内新旧对象的键不能冲突。九、对象数达到 max_objects 再添加抛 SORT_LIMIT；layer 只允许 0 到 255，越界抛 SORT_LAYER_INVALID；object_id 或 material 为空抛 SORT_OBJECT_INVALID；重复添加抛 SORT_DUPLICATE_OBJECT；未知对象查询、改深度、删除抛 SORT_UNKNOWN_OBJECT。十、stats 固定五个整数：frames、reused_order、reordered_objects、degraded、depth_updates；reordered_objects 累计位置发生变化的绘制条目，新条目也计入；reset_stats 只清计数。十一、snapshot 覆盖 version、objects（含 layer、material、批次、depth、桶、稳定序号、keys）、materials、counter、上一帧顺序与桶快照、stats，必须是深拷贝；restore 遇非法结构抛 SORT_SNAPSHOT_INVALID 且原状态不变，合法恢复后同一帧内键不得冲突。

硬约束：排序键全局唯一且固定 64 位，稳定序号与双面拆分的位域不得互相覆盖；单帧重排条目受 sort_budget 限制，超出必须走粗排，不允许无界全量排序；快照跨进程恢复后不得与恢复前的对象表共享可变结构，恢复后的键与顺序必须可继续使用。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；增量与恢复场景自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 图形渲染
- type: Bugfix
- module: transparent_sort_guard
- typeName: TransparentSortGuard
